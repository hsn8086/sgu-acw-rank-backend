"""Administrative endpoints: invites, member management, the admin-only note.

Reaching any route here requires the ``admin`` or ``root`` role. Root-only
routes are marked as such; they cover operations that must not spread sideways,
like promoting someone to root or touching a root account.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from ..cleanup import delete_user_accounts, delete_user_references
from ..db import get_session
from ..deps import require_admin
from ..models import Account, Invite, Role, User, UserSession, UserStatus, utcnow
from ..schemas import (
    AdminCreateUserRequest,
    AdminUserPage,
    InviteCreate,
    InvitePublic,
    UpdateUserAdminRequest,
    UserAdmin,
)
from ..security import hash_password, new_invite_code, new_temporary_password
from ..student_id import StudentIdError, parse_student_id

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


# --------------------------------------------------------------------------- #
# Invites
# --------------------------------------------------------------------------- #


@router.get("/invites", response_model=list[InvitePublic])
def list_invites(
    include_dead: bool = Query(default=True, description="是否包含已失效的邀请码"),
    db: Session = Depends(get_session),
) -> list[Invite]:
    statement = select(Invite).order_by(Invite.created_at.desc())  # type: ignore[attr-defined]
    rows = list(db.exec(statement).all())
    if not include_dead:
        rows = [row for row in rows if row.is_usable]
    return rows


@router.post("/invites", response_model=InvitePublic, status_code=status.HTTP_201_CREATED)
def create_invite(
    payload: InviteCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> Invite:
    """Mint an invite code.

    Only root may hand out administrative invites; an admin issuing an admin
    code would be privilege escalation by proxy.
    """
    if payload.grants_role == Role.ROOT:
        raise HTTPException(status_code=403, detail="不能通过邀请码授予 root")
    if payload.grants_role == Role.ADMIN and not admin.is_root:
        raise HTTPException(status_code=403, detail="只有 root 可以创建管理员邀请码")

    # Retry on the vanishingly unlikely collision rather than trusting luck.
    for _ in range(5):
        code = new_invite_code()
        if db.exec(select(Invite).where(Invite.code == code)).first() is None:
            break
    else:  # pragma: no cover - would need a broken RNG
        raise HTTPException(status_code=500, detail="无法生成邀请码")

    invite = Invite(
        code=code,
        grants_role=payload.grants_role,
        max_uses=payload.max_uses,
        use_count=0,
        expires_at=utcnow() + timedelta(hours=payload.ttl_hours),
        note=payload.note,
        created_by=admin.id,
        created_at=utcnow(),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite


@router.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invite(
    invite_id: int,
    db: Session = Depends(get_session),
) -> Response:
    """Revoke an invite without deleting it, so the audit trail survives."""
    invite = db.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    if invite.revoked_at is None:
        invite.revoked_at = utcnow()
        db.add(invite)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #


@router.get("/users", response_model=AdminUserPage)
def list_users(
    db: Session = Depends(get_session),
    q: str | None = Query(default=None, description="按学号或姓名模糊搜索"),
    role: str | None = Query(default=None),
    status: str | None = Query(default=None),
    enroll_year: int | None = Query(default=None),
    college_code: str | None = Query(default=None),
    has_account: bool | None = Query(default=None, description="是否已绑定平台账号"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    sort: str = Query(
        default="enroll_year", description="enroll_year|student_id|display_name|created_at"
    ),
) -> AdminUserPage:
    """成员列表，支持搜索、筛选、排序与分页。

    过滤都在数据库里做，不是取全部再在 Python 里筛——成员多起来之后
    后者会随人数线性变慢。
    """
    statement = select(User)

    if q and q.strip():
        # 学号按前缀匹配，姓名按包含匹配：学号输一半也能搜到。
        needle = q.strip()
        statement = statement.where(
            or_(User.student_id.startswith(needle), User.display_name.contains(needle))  # type: ignore[union-attr]
        )

    if role:
        if role not in Role.ALL:
            raise HTTPException(status_code=400, detail="未知的角色")
        statement = statement.where(User.role == role)

    if status:
        statement = statement.where(User.status == status)

    if enroll_year is not None:
        statement = statement.where(User.enroll_year == enroll_year)

    if college_code:
        statement = statement.where(User.college_code == college_code)

    if has_account is not None:
        # 用子查询而不是 join：一个人绑了多个账号时 join 会出现重复行。
        bound = select(Account.user_id).where(Account.verified_at.is_not(None))  # type: ignore[union-attr]
        if has_account:
            statement = statement.where(User.id.in_(bound))  # type: ignore[attr-defined]
        else:
            statement = statement.where(User.id.not_in(bound))  # type: ignore[attr-defined]

    # 总数要在分页之前算。
    total = len(db.exec(statement).all())

    sort_columns = {
        "enroll_year": (User.enroll_year.desc(), User.student_id),  # type: ignore[union-attr]
        "student_id": (User.student_id,),
        "display_name": (User.display_name,),
        "created_at": (User.created_at.desc(),),  # type: ignore[union-attr]
    }
    order_by = sort_columns.get(sort, sort_columns["enroll_year"])
    statement = statement.order_by(*order_by).offset((page - 1) * page_size).limit(page_size)

    items = list(db.exec(statement).all())

    # 供前端渲染筛选下拉，免得选项写死在两处。
    years = sorted(
        {y for y in db.exec(select(User.enroll_year).distinct()).all() if y},  # type: ignore[attr-defined]
        reverse=True,
    )
    colleges = sorted({c for c in db.exec(select(User.college_code).distinct()).all() if c})  # type: ignore[attr-defined]

    return AdminUserPage(
        items=[UserAdmin.model_validate(u) for u in items],
        total=total,
        page=page,
        page_size=page_size,
        enroll_years=years,
        college_codes=colleges,
    )


@router.post("/users", response_model=UserAdmin, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: AdminCreateUserRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> User:
    """管理员直接建号，不用邀请码。

    权限规则与邀请码一致：不能造 root，只有 root 能造管理员。
    """
    if payload.role == Role.ROOT:
        raise HTTPException(status_code=403, detail="不能直接创建 root 账号")
    if payload.role == Role.ADMIN and not admin.is_root:
        raise HTTPException(status_code=403, detail="只有 root 可以创建管理员")

    try:
        parsed = parse_student_id(payload.student_id)
    except StudentIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if db.exec(select(User).where(User.student_id == parsed.raw)).first() is not None:
        raise HTTPException(status_code=409, detail="该学号已注册")

    user = User(
        student_id=parsed.raw,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        enroll_year=parsed.enroll_year,
        level=parsed.level,
        college_code=parsed.college_code,
        major_code=parsed.major_code,
        role=payload.role,
        status=UserStatus.ACTIVE,
        admin_note=payload.admin_note,
        created_at=utcnow(),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该学号已注册") from exc
    db.refresh(user)
    return user


@router.get("/users/{user_id}", response_model=UserAdmin)
def get_user(user_id: int, db: Session = Depends(get_session)) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    return user


@router.patch("/users/{user_id}", response_model=UserAdmin)
def update_user(
    user_id: int,
    payload: UpdateUserAdminRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> User:
    """Edit a member's role, status, or the admin-only note.

    ``admin_note`` is writable here and readable only from admin endpoints;
    the member-facing schemas do not carry the field at all.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="成员不存在")

    if payload.role is not None and payload.role != user.role:
        if user.is_root:
            raise HTTPException(status_code=403, detail="不能修改 root 的角色")
        if payload.role == Role.ROOT and not admin.is_root:
            raise HTTPException(status_code=403, detail="只有 root 可以授予 root")
        if payload.role == Role.ADMIN and not admin.is_root:
            raise HTTPException(status_code=403, detail="只有 root 可以授予管理员")
        user.role = payload.role

    if payload.status is not None and payload.status != user.status:
        if user.is_root:
            raise HTTPException(status_code=403, detail="不能停用 root")
        user.status = payload.status
        if payload.status == UserStatus.DISABLED:
            # A disabled account should not keep working sessions around.
            for row in db.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
                db.delete(row)

    if payload.admin_note is not None:
        user.admin_note = payload.admin_note

    if payload.display_name is not None:
        user.display_name = payload.display_name

    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> Response:
    """删除成员，连同其会话与平台账号。

    只能删成员：管理员和 root 要先降级再删，避免一次误操作把管理员清空。
    也不能删自己，否则会当场把自己踢出去。
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    if user.is_root:
        raise HTTPException(status_code=403, detail="不能删除 root 账号")
    if user.role == Role.ADMIN and not admin.is_root:
        raise HTTPException(status_code=403, detail="只有 root 可以删除管理员")

    # 会话、账号、以及指向该成员的其他记录都要一起清掉。漏掉任何一张带
    # 外键的表，删除都会被数据库拦下。
    for row in db.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
        db.delete(row)
    delete_user_accounts(db, user.id)
    delete_user_references(db, user.id)
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> dict:
    """Reset a member's password to a random value and evict their sessions.

    There is no self-service recovery: accounts are keyed on a student id with
    no email attached, so a forgotten password means asking an administrator.
    The new password is returned exactly once, in this response, and is not
    recoverable afterwards.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    if user.is_root and not admin.is_root:
        raise HTTPException(status_code=403, detail="只有 root 可以重置 root 的密码")

    temporary = new_temporary_password()
    user.password_hash = hash_password(temporary)
    db.add(user)
    for row in db.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
        db.delete(row)
    db.commit()

    return {
        "student_id": user.student_id,
        "display_name": user.display_name,
        "temporary_password": temporary,
    }


@router.get("/stats")
def stats(db: Session = Depends(get_session)) -> dict:
    """Counts for the admin dashboard."""
    total = db.exec(select(func.count()).select_from(User)).one()
    active = db.exec(
        select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE)
    ).one()
    admins = db.exec(
        select(func.count()).select_from(User).where(User.role.in_(Role.ADMINISTRATIVE))  # type: ignore[attr-defined]
    ).one()
    live_invites = [row for row in db.exec(select(Invite)).all() if row.is_usable]
    return {
        "user_count": int(total),
        "active_count": int(active),
        "admin_count": int(admins),
        "usable_invite_count": len(live_invites),
    }
