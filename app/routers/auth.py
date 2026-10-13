"""Registration, login, logout and self-service."""

from __future__ import annotations

import io
from datetime import timedelta

import pyotp
import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlmodel import Session, select

from ..config import get_settings
from ..db import get_session
from ..deps import get_current_user, get_optional_user, require_origin_secret
from ..models import Invite, InviteUse, User, UserSession, UserStatus, utcnow
from ..schemas import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    UpdateSelfRequest,
    UserSelf,
)
from ..security import (
    hash_password,
    hash_session_token,
    new_login_ticket,
    new_session_token,
    verify_login_ticket,
    verify_password,
)
from ..student_id import StudentIdError, parse_student_id

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    dependencies=[Depends(require_origin_secret)],
)

#: Verified against when a login names an unknown student id, so the failure
#: path costs the same as a real one. Computed once at import: hashing on every
#: miss would do the scrypt work twice and leak timing the other way.
_DUMMY_HASH = hash_password("timing-equaliser-never-matches")


def _issue_session(db: Session, user: User, request: Request) -> str:
    """Create a session row and return the raw token for the cookie."""
    settings = get_settings()
    token = new_session_token()
    db.add(
        UserSession(
            token_hash=hash_session_token(token),
            user_id=user.id,
            expires_at=utcnow() + timedelta(days=settings.session_ttl_days),
            ip=(request.client.host if request.client else "")[:64],
            user_agent=(request.headers.get("user-agent") or "")[:256],
        )
    )
    return token


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.session_ttl_days * 24 * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain or None,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.cookie_name,
        domain=settings.cookie_domain or None,
        path="/",
    )


def _claim_invite(db: Session, code: str) -> Invite:
    """Atomically consume one use of ``code``.

    The increment is expressed as a conditional UPDATE so that SQLite's
    single-writer guarantee decides the winner: two concurrent redemptions of
    the last remaining slot produce rowcounts of 1 and 0, never 1 and 1. Doing
    the check and the increment in Python instead would let both succeed.
    """
    now = utcnow()
    result = db.exec(  # type: ignore[call-overload]
        update(Invite)
        .where(
            Invite.code == code,
            Invite.use_count < Invite.max_uses,
            Invite.revoked_at.is_(None),  # type: ignore[union-attr]
            Invite.expires_at > now,
        )
        .values(use_count=Invite.use_count + 1)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=400, detail="邀请码无效或已用尽")

    invite = db.exec(select(Invite).where(Invite.code == code)).one()
    return invite


@router.post("/register", response_model=UserSelf, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
) -> User:
    try:
        parsed = parse_student_id(payload.student_id)
    except StudentIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        invite = _claim_invite(db, payload.invite_code.strip().upper())

        existing = db.exec(select(User).where(User.student_id == parsed.raw)).first()
        if existing is not None:
            # Rolls back the use_count increment taken above.
            raise HTTPException(status_code=409, detail="该学号已注册")

        user = User(
            student_id=parsed.raw,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name,
            enroll_year=parsed.enroll_year,
            level=parsed.level,
            college_code=parsed.college_code,
            major_code=parsed.major_code,
            class_no=parsed.class_no,
            role=invite.grants_role,
            status=UserStatus.ACTIVE,
            created_at=utcnow(),
        )
        db.add(user)
        db.flush()

        # 记下是谁用的这个码。use_count 只知道用了几次，出事时（比如码被
        # 转发到群外）光看次数查不出人来。
        db.add(InviteUse(invite_id=invite.id, user_id=user.id, used_at=utcnow()))

        token = _issue_session(db, user, request)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    db.refresh(user)
    _set_session_cookie(response, token)
    return user


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
) -> dict:
    student_id = payload.student_id.strip().upper()
    user = db.exec(select(User).where(User.student_id == student_id)).first()

    # Same message either way, so the response does not reveal which student ids
    # exist. verify_password still runs against a dummy hash when the user is
    # missing, keeping the timing comparable.
    if user is None:
        verify_password(payload.password, _DUMMY_HASH)
        raise HTTPException(status_code=401, detail="学号或密码错误")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="学号或密码错误")
    if user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="账号已停用")

    # 开了两步验证：先给一张签名的登录票据，等 TOTP 验完才发真 session。
    if user.totp_secret:
        return {
            "totp_required": True,
            "ticket": new_login_ticket(user.id or 0),
        }

    user.last_login_at = utcnow()
    db.add(user)
    token = _issue_session(db, user, request)
    db.commit()
    db.refresh(user)

    _set_session_cookie(response, token)
    return {"totp_required": False, "user": UserSelf.model_validate(user).model_dump()}


class TotpLoginIn(BaseModel):
    ticket: str
    code: str = Field(min_length=6, max_length=8)


@router.post("/login/totp")
def login_totp(
    payload: TotpLoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
) -> dict:
    """第二步：票据 + TOTP 码 → 真 session。"""
    uid = verify_login_ticket(payload.ticket)
    if uid is None:
        raise HTTPException(status_code=401, detail="票据过期或无效，请重新登录")
    user = db.get(User, uid)
    if user is None or not user.totp_secret:
        raise HTTPException(status_code=401, detail="票据过期或无效，请重新登录")
    if user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="账号已停用")
    if not pyotp.TOTP(user.totp_secret).verify(payload.code, valid_window=1):
        raise HTTPException(status_code=401, detail="验证码不对")

    user.last_login_at = utcnow()
    db.add(user)
    token = _issue_session(db, user, request)
    db.commit()
    db.refresh(user)
    _set_session_cookie(response, token)
    return {"totp_required": False, "user": UserSelf.model_validate(user).model_dump()}


# --------------------------------------------------------------------------- #
# TOTP 两步验证的开启 / 关闭（已登录成员自助）
# --------------------------------------------------------------------------- #


class TotpCodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


@router.post("/me/totp/begin")
def totp_begin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    """生成待验证的 TOTP 密钥 + otpauth URI + QR（SVG 字符串）。

    密钥落在 ``totp_pending_secret``，等 /me/totp/enable 验过码才转正——
    直接转正的话，二维码没扫进去的人会被锁在自己账号外。
    """
    secret = pyotp.random_base32()
    user.totp_pending_secret = secret
    db.add(user)
    db.commit()

    uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.student_id, issuer_name=get_settings().rp_name
    )
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": buf.getvalue().decode()}


@router.post("/me/totp/enable")
def totp_enable(
    payload: TotpCodeIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    """验证一个码，把 pending 密钥转正。"""
    if not user.totp_pending_secret:
        raise HTTPException(status_code=400, detail="没有待开启的两步验证，先调用 begin")
    if not pyotp.TOTP(user.totp_pending_secret).verify(payload.code, valid_window=1):
        raise HTTPException(status_code=422, detail="验证码不对")
    user.totp_secret = user.totp_pending_secret
    user.totp_pending_secret = None
    db.add(user)
    db.commit()
    return {"ok": True}


@router.post("/me/totp/disable")
def totp_disable(
    payload: TotpCodeIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    """关掉两步验证——要先验一次码，免得拿到会话的人随手关掉别人的。"""
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="没有开启两步验证")
    if not pyotp.TOTP(user.totp_secret).verify(payload.code, valid_window=1):
        raise HTTPException(status_code=422, detail="验证码不对")
    user.totp_secret = None
    user.totp_pending_secret = None
    db.add(user)
    db.commit()
    return {"ok": True}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
) -> Response:
    """Drop the current session. Idempotent."""
    settings = get_settings()
    token = request.cookies.get(settings.cookie_name)
    if token:
        row = db.exec(
            select(UserSession).where(UserSession.token_hash == hash_session_token(token))
        ).first()
        if row is not None:
            db.delete(row)
            db.commit()
    _clear_session_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserSelf)
def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.get("/session")
def session(
    user: User | None = Depends(get_optional_user),
) -> dict:
    """当前会话，未登录时返回 ``{"user": null}`` 而非 401。

    前端每次加载都要问「我是谁」，用 /auth/me 的话未登录会返回 401，浏览器
    控制台会把每个 401 记成错误。这里始终返回 200，让「未登录」和「出错」
    在客户端能区分开。需要鉴权的接口仍然用 /auth/me。
    """
    return {"user": UserSelf.model_validate(user) if user else None}


@router.patch("/me", response_model=UserSelf)
def update_me(
    payload: UpdateSelfRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> User:
    if payload.display_name is not None:
        user.display_name = payload.display_name
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> Response:
    """Change one's own password.

    Every other session is evicted so a stolen cookie stops working; the caller
    keeps a fresh one and stays logged in.
    """
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")

    settings = get_settings()
    current_token = request.cookies.get(settings.cookie_name) or ""
    current_hash = hash_session_token(current_token)

    user.password_hash = hash_password(payload.new_password)
    db.add(user)

    for row in db.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
        if row.token_hash != current_hash:
            db.delete(row)
    db.commit()

    # The caller's own session survives, so their cookie stays valid and no
    # re-login is needed.
    return Response(status_code=status.HTTP_204_NO_CONTENT)
