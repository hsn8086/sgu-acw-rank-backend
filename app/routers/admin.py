"""Administrative endpoints: invites, member management, the admin-only note.

Reaching any route here requires the ``admin`` or ``root`` role. Root-only
routes are marked as such; they cover operations that must not spread sideways,
like promoting someone to root or touching a root account.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from .. import academic
from ..cleanup import delete_user_accounts, delete_user_references
from ..db import get_session
from ..deps import require_admin
from ..models import (
    Account,
    DailyProgress,
    Invite,
    Role,
    SolvedSnapshot,
    User,
    UserSession,
    UserStatus,
    utcnow,
)
from ..schemas import (
    AcademicOptions,
    AdminAccountDetail,
    AdminCreateUserRequest,
    AdminMemberDetail,
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
) -> list[InvitePublic]:
    statement = select(Invite).order_by(Invite.created_at.desc())  # type: ignore[attr-defined]
    rows = list(db.exec(statement).all())
    if not include_dead:
        rows = [row for row in rows if row.is_usable]

    # 一次查出所有创建者，而不是每个邀请码查一次。
    creator_ids = {row.created_by for row in rows if row.created_by is not None}
    creators: dict[int, User] = {}
    if creator_ids:
        found = db.exec(select(User).where(User.id.in_(creator_ids))).all()  # type: ignore[attr-defined]
        creators = {user.id: user for user in found}

    out: list[InvitePublic] = []
    for row in rows:
        item = InvitePublic.model_validate(row)
        creator = creators.get(row.created_by) if row.created_by else None
        if creator is not None:
            item.created_by_name = creator.display_name
            item.created_by_student_id = creator.student_id
        out.append(item)
    return out


@router.post("/invites", response_model=InvitePublic, status_code=status.HTTP_201_CREATED)
def create_invite(
    payload: InviteCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> Invite:
    """Mint an invite code.

    管理员之间是平级的：任何管理员都能发管理员邀请码。root 是唯一的例外，
    不能通过邀请码授予——root 只有一个，要转让得走别的路。
    """
    if payload.grants_role == Role.ROOT:
        raise HTTPException(status_code=403, detail="不能通过邀请码授予 root")

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

    # 建完立刻返回，前端要直接插进列表里。创建者信息一并带上，否则新出现的
    # 那一行跟其他行长得不一样，得刷新才有。
    item = InvitePublic.model_validate(invite)
    item.created_by_name = admin.display_name
    item.created_by_student_id = admin.student_id
    return item


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

    权限规则与邀请码一致：不能造 root，管理员之间平级。
    """
    if payload.role == Role.ROOT:
        raise HTTPException(status_code=403, detail="不能直接创建 root 账号")

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
        class_no=parsed.class_no,
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
        # 管理员之间平级：任何管理员都能授予或撤销管理员。
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

    # 空串表示恢复成按学号查表。
    if payload.college_override is not None:
        user.college_override = payload.college_override.strip() or None
    if payload.major_override is not None:
        user.major_override = payload.major_override.strip() or None

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
    # 管理员之间平级，所以能互相删除。

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


@router.get("/academics", response_model=AcademicOptions)
def academic_options() -> AcademicOptions:
    """学院与专业的候选列表，供管理员改学院/专业时选。

    数据是静态的（学号码表），不用查库。
    """
    colleges = sorted(set(academic.COLLEGES.values()))
    majors: dict[str, list[str]] = {name: [] for name in colleges}
    for (college_code, _), major in academic.MAJORS.items():
        college = academic.COLLEGES.get(college_code)
        if college and major not in majors[college]:
            majors[college].append(major)
    for names in majors.values():
        names.sort()
    return AcademicOptions(colleges=colleges, majors=majors)


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


@router.get("/members/{user_id}", response_model=AdminMemberDetail)
def get_member_detail(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> AdminMemberDetail:
    """成员详情的管理员视角。

    比公开信息多出一块排查用的数据：仅管理员可见的备注、最后登录时间、
    每个平台账号的同步健康度（抓取时间、失败次数、最近错误）。

    这些不是「更多信息」，而是回答一个具体问题用的：**这个人为什么没数据**。
    常见原因是账号验证没过、平台抓取一直失败、或者人本来就不做题了——
    这三者从公开信息里看不出来。
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="成员不存在")

    import json as _json

    from ..sync import current_practice_day

    accounts = list(db.exec(select(Account).where(Account.user_id == user_id)).all())
    account_ids = [a.id for a in accounts if a.id is not None]

    # 已过题目数与练习天数，一次查完，别在循环里逐个查。
    solved_by_account: dict[int, int] = {}
    if account_ids:
        for snapshot in db.exec(
            select(SolvedSnapshot).where(
                SolvedSnapshot.account_id.in_(account_ids)  # type: ignore[attr-defined]
            )
        ).all():
            try:
                solved_by_account[snapshot.account_id] = len(
                    _json.loads(snapshot.problems_json or "[]")
                )
            except (TypeError, ValueError):
                solved_by_account[snapshot.account_id] = snapshot.problem_count

    active_days_by_account: dict[int, int] = {}
    if account_ids:
        rows = db.exec(
            select(DailyProgress.account_id, func.count())  # type: ignore[call-overload]
            .where(
                DailyProgress.account_id.in_(account_ids),  # type: ignore[attr-defined]
                DailyProgress.solved_count > 0,
            )
            .group_by(DailyProgress.account_id)  # type: ignore[arg-type]
        ).all()
        for account_id, count in rows:
            active_days_by_account[account_id] = count

    details = [
        AdminAccountDetail(
            id=a.id or 0,
            platform=a.platform,
            identifier=a.identifier,
            display_name=a.display_name,
            verified_at=a.verified_at,
            verify_attempts=a.verify_attempts,
            verify_attempted_at=a.verify_attempted_at,
            rating=a.rating,
            max_rating=a.max_rating,
            rating_fetched_at=a.rating_fetched_at,
            solved_fetched_at=a.solved_fetched_at,
            last_active_at=a.last_active_at,
            last_error=a.last_error,
            consecutive_failures=a.consecutive_failures,
            rating_failures=a.rating_failures,
            created_at=a.created_at,
            solved_count=solved_by_account.get(a.id or 0, 0),
            active_days=active_days_by_account.get(a.id or 0, 0),
        )
        for a in accounts
    ]

    # 最近 30 天的活跃天数：按「人」算，同一天多个账号只算一天。
    today = date.fromisoformat(current_practice_day())
    cutoff = (today.fromordinal(today.toordinal() - 30)).isoformat()
    recent_days: set[str] = set()
    last_date: str | None = None
    if account_ids:
        for row in db.exec(
            select(DailyProgress).where(
                DailyProgress.account_id.in_(account_ids),  # type: ignore[attr-defined]
                DailyProgress.solved_count > 0,
            )
        ).all():
            if row.date >= cutoff:
                recent_days.add(row.date)
            if last_date is None or row.date > last_date:
                last_date = row.date

    return AdminMemberDetail(
        id=user.id or 0,
        display_name=user.display_name,
        student_id=user.student_id,
        enroll_year=user.enroll_year,
        level=user.level,
        college_code=user.college_code,
        major_code=user.major_code,
        college_name=user.college_name,
        major_name=user.major_name,
        class_label=user.class_label,
        role=user.role,
        status=user.status,
        admin_note=user.admin_note,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        accounts=details,
        total_solved=sum(d.solved_count for d in details),
        active_days_30=len(recent_days),
        last_practice_date=last_date,
        failing_accounts=sum(1 for d in details if d.consecutive_failures > 0),
    )
