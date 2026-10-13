"""集训的接口。

对外的：列表、详情、报名、取消报名。
对管理员的：增删改、加题删题、手动加人、导出。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import AwareDatetime, BaseModel, Field, StringConstraints
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from .. import camps as camps_mod
from ..deps import get_current_user, get_optional_user, get_session, require_admin
from ..models import (
    Camp,
    CampAccountResult,
    CampFinalization,
    CampProblem,
    CampRegistration,
    Platform,
    Role,
    User,
)
from ..problems import normalize_key
from .problemlists import _fetch_and_cache

router = APIRouter(tags=["camps"])


# --------------------------------------------------------------------------- #
# 出参模型
# --------------------------------------------------------------------------- #


class CampProblemOut(BaseModel):
    platform: str
    key: str
    name: str = ""
    rating: int | None = None
    url: str = ""
    #: 当前查看的人过了没有。未报名拿不到这个字段——题面本身都不给看。
    done: bool = False


class CampLeaderboardEntry(BaseModel):
    rank: int
    user_id: int
    display_name: str
    student_id: str
    enroll_year: int
    class_label: str | None = None
    solved_count: int
    per_problem: dict[str, bool]


class CampResultStatus(BaseModel):
    phase: str
    finalized_at: datetime | None = None
    pending_accounts: int = 0


class CampDetail(CampResultStatus):
    id: int
    name: str
    description: str
    start_at: datetime
    end_at: datetime
    started: bool
    ended: bool
    #: 生效的开放报名/开始显示时间。
    register_open: datetime | None = None
    visible_from: datetime | None = None
    registered: bool
    registration_count: int
    problem_count: int
    platforms: list[str]
    can_register: bool
    can_unregister: bool
    register_reason: str
    problems: list[CampProblemOut]
    leaderboard: list[CampLeaderboardEntry]


class CampSummary(BaseModel):
    id: int
    name: str
    description: str
    start_at: datetime
    end_at: datetime
    started: bool
    ended: bool
    #: 生效的开放报名/开始显示时间。``None`` 表示不设限制。
    register_open: datetime | None = None
    visible_from: datetime | None = None
    registration_count: int
    problem_count: int


class AdminCampProblem(BaseModel):
    id: int
    platform: str
    key: str
    position: int
    #: 题名。管理员在这边也要看见，不然加错题看不出来。
    name: str = ""
    rating: int | None = None


class AdminCampDetail(CampSummary, CampResultStatus):
    created_by: int
    #: 编辑表单用的原始字段：``_at`` 非空 = 绝对模式，否则按 ``_days`` 回推。
    register_open_at: datetime | None = None
    register_open_days: int | None = None
    visible_at: datetime | None = None
    visible_days: int | None = None
    problems: list[AdminCampProblem]
    registrations: list[dict]
    #: 最终榜单——管理页要能看到谁过了什么，不用翻到前台页去。
    leaderboard: list[CampLeaderboardEntry]


# --------------------------------------------------------------------------- #
# 出参拼装
# --------------------------------------------------------------------------- #


def _to_summary(
    camp: Camp,
    session: Session,
    *,
    registration_count: int | None = None,
    problem_count: int | None = None,
) -> CampSummary:
    now = datetime.now(UTC)
    if registration_count is None:
        registration_count = len(
            session.exec(select(CampRegistration).where(CampRegistration.camp_id == camp.id)).all()
        )
    if problem_count is None:
        problem_count = len(
            session.exec(select(CampProblem).where(CampProblem.camp_id == camp.id)).all()
        )
    return CampSummary(
        id=camp.id or 0,
        name=camp.name,
        description=camp.description,
        start_at=camp.start_at,
        end_at=camp.end_at,
        started=now >= camp.start_at,
        ended=now >= camp.end_at,
        register_open=camps_mod.register_open_from(camp),
        visible_from=camps_mod.visible_from(camp),
        registration_count=registration_count,
        problem_count=problem_count,
    )


def _summaries(rows: list[Camp], session: Session) -> list[CampSummary]:
    """Build a list without querying registrations/problems once per camp."""
    if not rows:
        return []

    camp_ids = [camp.id for camp in rows]
    registration_counts = {
        camp_id: count
        for camp_id, count in session.exec(
            select(CampRegistration.camp_id, func.count(CampRegistration.id))
            .where(CampRegistration.camp_id.in_(camp_ids))
            .group_by(CampRegistration.camp_id)
        ).all()
    }
    problem_counts = {
        camp_id: count
        for camp_id, count in session.exec(
            select(CampProblem.camp_id, func.count(CampProblem.id))
            .where(CampProblem.camp_id.in_(camp_ids))
            .group_by(CampProblem.camp_id)
        ).all()
    }
    return [
        _to_summary(
            camp,
            session,
            registration_count=registration_counts.get(camp.id, 0),
            problem_count=problem_counts.get(camp.id, 0),
        )
        for camp in rows
    ]


def _to_detail(data: dict) -> CampDetail:
    return CampDetail(**data)


# --------------------------------------------------------------------------- #
# 对外的接口
# --------------------------------------------------------------------------- #


@router.get("/camps", response_model=list[CampSummary])
def list_camps(session: Session = Depends(get_session)) -> list[CampSummary]:
    """所有集训，进行中的在前；还没到显示时间的不出来。"""
    now = datetime.now(UTC)
    rows = [
        c
        for c in session.exec(select(Camp).order_by(Camp.start_at.desc())).all()
        if (v := camps_mod.visible_from(c)) is None or now >= v
    ]
    return _summaries(rows, session)


@router.get("/camps/{camp_id}", response_model=CampDetail)
def get_camp(
    camp_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(get_optional_user),
) -> CampDetail:
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    # 还没到显示时间的集训等同不存在——管理员可以在管理页看。
    visible = camps_mod.visible_from(camp)
    is_admin = user is not None and user.role in Role.ADMINISTRATIVE
    if visible is not None and datetime.now(UTC) < visible and not is_admin:
        raise HTTPException(status_code=404, detail="没有这份集训")
    data = camps_mod.detail_for_user(session, camp, user)
    return _to_detail(data)


@router.post("/camps/{camp_id}/register", status_code=status.HTTP_201_CREATED)
def register(
    camp_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    try:
        camps_mod.register(session, camp=camp, user_id=user.id or 0)
    except camps_mod.CampError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/camps/{camp_id}/unregister", status_code=status.HTTP_200_OK)
def unregister(
    camp_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    try:
        camps_mod.unregister(session, camp=camp, user_id=user.id or 0)
    except camps_mod.CampError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 管理员的接口
# --------------------------------------------------------------------------- #


CampName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class CampCreate(BaseModel):
    name: CampName
    description: str = Field(default="", max_length=5000)
    start_at: AwareDatetime
    end_at: AwareDatetime
    #: 开放报名/开始显示：传 ``_at`` 是绝对时间，传 ``_days`` 是开始前偏移。
    register_open_at: AwareDatetime | None = None
    register_open_days: int | None = Field(default=None, ge=0, le=60)
    visible_at: AwareDatetime | None = None
    visible_days: int | None = Field(default=None, ge=0, le=60)


class CampUpdate(BaseModel):
    name: CampName | None = None
    description: str | None = Field(default=None, max_length=5000)
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    register_open_at: AwareDatetime | None = None
    register_open_days: int | None = Field(default=None, ge=0, le=60)
    visible_at: AwareDatetime | None = None
    visible_days: int | None = Field(default=None, ge=0, le=60)


class ProblemAdd(BaseModel):
    platform: str = Field(min_length=1, max_length=16)
    key: str = Field(min_length=1, max_length=128)
    position: int = 0


class MemberAdd(BaseModel):
    user_id: int


@router.get("/admin/camps", response_model=list[CampSummary])
def admin_list(
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> list[CampSummary]:
    rows = session.exec(select(Camp).order_by(Camp.start_at.desc())).all()
    return _summaries(rows, session)


@router.post("/admin/camps", status_code=status.HTTP_201_CREATED, response_model=CampSummary)
def admin_create(
    payload: CampCreate,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin),
) -> CampSummary:
    if payload.end_at <= payload.start_at:
        raise HTTPException(status_code=400, detail="截止时间得晚于开始时间")
    if payload.register_open_at is not None and payload.register_open_days is not None:
        raise HTTPException(status_code=422, detail="开放报名只能选一种模式")
    if payload.visible_at is not None and payload.visible_days is not None:
        raise HTTPException(status_code=422, detail="开始显示只能选一种模式")
    camp = Camp(
        name=payload.name,
        description=payload.description,
        start_at=payload.start_at,
        end_at=payload.end_at,
        register_open_at=payload.register_open_at,
        # 默认开始前 3 天；传了绝对时间就切到绝对模式。
        register_open_days=(
            None
            if payload.register_open_at is not None
            else (payload.register_open_days if payload.register_open_days is not None else 3)
        ),
        visible_at=payload.visible_at,
        visible_days=(
            None
            if payload.visible_at is not None
            else (payload.visible_days if payload.visible_days is not None else 3)
        ),
        created_by=admin.id or 0,
    )
    session.add(camp)
    session.commit()
    session.refresh(camp)
    return _to_summary(camp, session)


def require_editable(camp: Camp) -> None:
    if datetime.now(UTC) >= camp.end_at:
        raise HTTPException(status_code=409, detail="集训已截止，题目、报名名单和时间已锁定")


def require_pre_start(camp: Camp) -> None:
    """开始前才能改的字段：开放报名和开始显示是「开始前」的概念，
    集训跑起来之后再调它们没有意义——反而能把进行中的集训藏没。"""
    if datetime.now(UTC) >= camp.start_at:
        raise HTTPException(status_code=409, detail="集训已开始，开放报名和显示时间已锁定")


@router.put("/admin/camps/{camp_id}", response_model=CampSummary)
def admin_update(
    camp_id: int,
    payload: CampUpdate,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> CampSummary:
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    time_fields = {
        "start_at",
        "end_at",
        "register_open_at",
        "register_open_days",
        "visible_at",
        "visible_days",
    }
    if payload.model_fields_set & time_fields:
        require_editable(camp)
    pre_start_fields = {"register_open_at", "register_open_days", "visible_at", "visible_days"}
    if payload.model_fields_set & pre_start_fields:
        require_pre_start(camp)
    if payload.name is not None:
        camp.name = payload.name
    if payload.description is not None:
        camp.description = payload.description
    if payload.start_at is not None:
        camp.start_at = payload.start_at
    if payload.end_at is not None:
        camp.end_at = payload.end_at
    if camp.end_at <= camp.start_at:
        raise HTTPException(status_code=400, detail="截止时间得晚于开始时间")
    times_changed = "start_at" in payload.model_fields_set or "end_at" in payload.model_fields_set
    # 模式互斥：传哪个用哪个，顺带清掉另一种。
    if "register_open_days" in payload.model_fields_set:
        camp.register_open_days = payload.register_open_days
        if payload.register_open_days is not None:
            camp.register_open_at = None
    if "register_open_at" in payload.model_fields_set:
        camp.register_open_at = payload.register_open_at
        if payload.register_open_at is not None:
            camp.register_open_days = None
    if "visible_days" in payload.model_fields_set:
        camp.visible_days = payload.visible_days
        if payload.visible_days is not None:
            camp.visible_at = None
    if "visible_at" in payload.model_fields_set:
        camp.visible_at = payload.visible_at
        if payload.visible_at is not None:
            camp.visible_days = None
    session.add(camp)
    # 只有起止时间变了才需要重同步账号；改名字不该触发全员重新抓取。
    if times_changed:
        camps_mod.boost_accounts(session, camp)
    session.commit()
    session.refresh(camp)
    return _to_summary(camp, session)


@router.delete("/admin/camps/{camp_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete(
    camp_id: int,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> Response:
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    for result in session.exec(
        select(CampAccountResult).where(CampAccountResult.camp_id == camp_id)
    ).all():
        session.delete(result)
    finalization = session.get(CampFinalization, camp_id)
    if finalization:
        session.delete(finalization)
    for p in session.exec(select(CampProblem).where(CampProblem.camp_id == camp_id)).all():
        session.delete(p)
    for r in session.exec(
        select(CampRegistration).where(CampRegistration.camp_id == camp_id)
    ).all():
        session.delete(r)
    # Camp 没有 relationship 指到子表，SQLAlchemy 的 Unit of Work 不知道该
    # 先删谁——flush 顺序不保证，万一 camps 的 DELETE 排在前面，子表还指着
    # 它，SQLite 立即报 FK constraint failed。先手动 flush 让子行删干净，
    # 再删 camp。
    session.flush()
    session.delete(camp)
    session.commit()
    return Response(status_code=204)


@router.get("/admin/camps/{camp_id}", response_model=AdminCampDetail)
def admin_detail(
    camp_id: int,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> AdminCampDetail:
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    problems = session.exec(
        select(CampProblem).where(CampProblem.camp_id == camp_id).order_by(CampProblem.position)
    ).all()
    registrations = session.exec(
        select(CampRegistration).where(CampRegistration.camp_id == camp_id)
    ).all()
    users = {
        u.id: u
        for u in session.exec(
            select(User).where(User.id.in_([r.user_id for r in registrations]))
        ).all()
    }
    lb_rows = camps_mod.standings(session, camp)
    meta = camps_mod.problem_meta(session, problems)

    def _problem_out(p: CampProblem) -> AdminCampProblem:
        row = meta.get((p.platform, p.key))
        return AdminCampProblem(
            id=p.id or 0,
            platform=p.platform,
            key=p.key,
            position=p.position,
            name=(row.name if row else "") or "",
            rating=row.rating if row else None,
        )

    return AdminCampDetail(
        **_to_summary(camp, session).model_dump(),
        **camps_mod.result_status(session, camp),
        created_by=camp.created_by,
        register_open_at=camp.register_open_at,
        register_open_days=camp.register_open_days,
        visible_at=camp.visible_at,
        visible_days=camp.visible_days,
        problems=[_problem_out(p) for p in problems],
        registrations=[
            {
                "user_id": r.user_id,
                "display_name": users.get(r.user_id).display_name if users.get(r.user_id) else "",
                "registered_at": r.registered_at.isoformat(),
            }
            for r in registrations
        ],
        leaderboard=[CampLeaderboardEntry(**row) for row in lb_rows],
    )


@router.post("/admin/camps/{camp_id}/problems", status_code=status.HTTP_201_CREATED)
def admin_add_problem(
    camp_id: int,
    payload: ProblemAdd,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> dict:
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    require_editable(camp)
    if payload.platform not in Platform.ALL:
        raise HTTPException(status_code=400, detail="未知的平台")
    key = normalize_key(payload.platform, payload.key)
    if not key or len(key) > 128:
        raise HTTPException(status_code=400, detail="请输入有效题号（最多 128 字符）")
    last = session.exec(
        select(CampProblem)
        .where(CampProblem.camp_id == camp_id)
        .order_by(CampProblem.position.desc())
        .limit(1)
    ).first()
    row = CampProblem(
        camp_id=camp_id,
        platform=payload.platform,
        # normalize_key 会把洛谷/CF 转大写、AtCoder 转小写、牛客加来源前缀。
        # 不规范化的话，过题记录里的 key 和这里对不上，排行榜永远 0。
        key=key,
        position=last.position + 1 if last else 0,
    )
    session.add(row)
    try:
        session.flush()
        camps_mod.boost_accounts(session, camp)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="这道题已经在集训中") from None
    # 顺手把题名抓下来——不抓的话 problem_cache 里没这条，列表和
    # 排行榜题号悬停都显示不了名字。
    _fetch_and_cache(session, payload.platform, key)
    return {"ok": True, "id": row.id}


class ReorderRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


@router.post("/admin/camps/{camp_id}/problems/reorder", response_model=list[AdminCampProblem])
def admin_reorder_problems(
    camp_id: int,
    payload: ReorderRequest,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> list[AdminCampProblem]:
    """拖拽排序集训里的题目。跟题单的 reorder 是一个模式：整个顺序提交。"""
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    require_editable(camp)
    rows = session.exec(select(CampProblem).where(CampProblem.camp_id == camp_id)).all()
    by_id = {row.id: row for row in rows}
    if len(payload.ids) != len(by_id) or set(payload.ids) != set(by_id):
        raise HTTPException(status_code=409, detail="题目列表已变化，请刷新后重新排序")
    for index, problem_id in enumerate(payload.ids):
        row = by_id[problem_id]
        row.position = index
        session.add(row)
    session.commit()
    rows = session.exec(
        select(CampProblem).where(CampProblem.camp_id == camp_id).order_by(CampProblem.position)
    ).all()
    meta = camps_mod.problem_meta(session, rows)
    out = []
    for p in rows:
        row = meta.get((p.platform, p.key))
        out.append(
            AdminCampProblem(
                id=p.id or 0,
                platform=p.platform,
                key=p.key,
                position=p.position,
                name=(row.name if row else "") or "",
                rating=row.rating if row else None,
            )
        )
    return out


@router.delete(
    "/admin/camps/{camp_id}/problems/{problem_id}", status_code=status.HTTP_204_NO_CONTENT
)
def admin_delete_problem(
    camp_id: int,
    problem_id: int,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> Response:
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    require_editable(camp)
    row = session.get(CampProblem, problem_id)
    if row is None or row.camp_id != camp_id:
        raise HTTPException(status_code=404, detail="没有这道题")
    session.delete(row)
    session.commit()
    return Response(status_code=204)


@router.post("/admin/camps/{camp_id}/members", status_code=status.HTTP_201_CREATED)
def admin_add_member(
    camp_id: int,
    payload: MemberAdd,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> dict:
    """管理员手动加人——不受「至少一个平台账号」的限制。"""
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    try:
        camps_mod.register(session, camp=camp, user_id=payload.user_id, admin=True)
    except camps_mod.CampError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.delete("/admin/camps/{camp_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_remove_member(
    camp_id: int,
    user_id: int,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> Response:
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")
    require_editable(camp)
    row = session.exec(
        select(CampRegistration).where(
            CampRegistration.camp_id == camp_id,
            CampRegistration.user_id == user_id,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="这个人没报名")
    session.delete(row)
    session.commit()
    return Response(status_code=204)


@router.get("/admin/camps/{camp_id}/export")
def admin_export(
    camp_id: int,
    format: str = Query(default="xlsx", pattern="^(xlsx|csv|json)$"),
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> Response:
    camp = session.get(Camp, camp_id)
    if camp is None:
        raise HTTPException(status_code=404, detail="没有这份集训")

    rows = camps_mod.standings(session, camp)
    problems = session.exec(
        select(CampProblem).where(CampProblem.camp_id == camp_id).order_by(CampProblem.position)
    ).all()
    headers = ["学院", "专业", "班级", "姓名", "学号", "洛谷号", "过题数"]
    problem_keys = [f"{p.platform}:{p.key}" for p in problems]
    headers.extend(f"{p.platform}:{p.key}" for p in problems)

    data = []
    for entry in rows:
        user = session.get(User, entry["user_id"])
        luogu = entry.get(
            "luogu",
            next(
                (a.identifier for a in user.accounts if a.platform == "luogu" and a.verified_at),
                "",
            )
            if user
            else "",
        )
        row = [
            entry.get("college_name") or "",
            entry.get("major_name") or "",
            entry.get("class_label") or "",
            entry["display_name"],
            entry["student_id"],
            luogu,
            str(entry["solved_count"]),
        ]
        row.extend("是" if entry["per_problem"].get(k) else "否" for k in problem_keys)
        data.append(row)

    if format == "json":
        import json

        return Response(
            content=json.dumps(
                {
                    "camp": {
                        "id": camp.id,
                        "name": camp.name,
                        **{
                            k: (v.isoformat() if isinstance(v, datetime) else v)
                            for k, v in camps_mod.result_status(session, camp).items()
                        },
                    },
                    "headers": headers,
                    "rows": data,
                },
                ensure_ascii=False,
            ),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="camp-{camp_id}.json"'},
        )

    if format == "csv":
        import csv
        import io

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(data)
        return Response(
            content="\ufeff" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="camp-{camp_id}.csv"'},
        )

    # xlsx
    import io as _io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in data:
        ws.append(row)
    buf = _io.BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="camp-{camp_id}.xlsx"'},
    )
