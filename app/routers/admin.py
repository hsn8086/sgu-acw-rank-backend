"""Administrative endpoints: invites, member management, the admin-only note.

Reaching any route here requires the ``admin`` or ``root`` role. Root-only
routes are marked as such; they cover operations that must not spread sideways,
like promoting someone to root or touching a root account.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlmodel import Session, func, select

from ..db import get_session
from ..deps import require_admin
from ..models import Invite, Role, User, UserSession, UserStatus, utcnow
from ..schemas import (
    InviteCreate,
    InvitePublic,
    UpdateUserAdminRequest,
    UserAdmin,
)
from ..security import hash_password, new_invite_code, new_temporary_password

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


@router.get("/users", response_model=list[UserAdmin])
def list_users(
    db: Session = Depends(get_session),
) -> list[User]:
    return list(db.exec(select(User).order_by(User.enroll_year.desc(), User.student_id)).all())  # type: ignore[attr-defined]


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

    db.add(user)
    db.commit()
    db.refresh(user)
    return user


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
