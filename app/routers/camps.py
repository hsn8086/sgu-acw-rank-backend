"""集训的接口。

对外的：列表、详情、报名、取消报名。
对管理员的：增删改、加题删题、手动加人、导出。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .. import camps as camps_mod
from ..deps import get_current_user, get_optional_user, get_session, require_admin
from ..models import Camp, CampProblem, CampRegistration, User

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


class CampLeaderboardEntry(BaseModel):
    rank: int
    user_id: int
    display_name: str
    student_id: str
    enroll_year: int
    class_label: str | None = None
    solved_count: int
    per_problem: dict[str, bool]


class CampDetail(BaseModel):
    id: int
    name: str
    description: str
    start_at: datetime
    end_at: datetime
    started: bool
    ended: bool
    registered: bool
    registration_count: int
    problem_count: int
    platforms: list[str]
    can_register: bool
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
    registration_count: int
    problem_count: int


class AdminCampProblem(BaseModel):
    id: int
    platform: str
    key: str
    position: int


class AdminCampDetail(CampSummary):
    created_by: int
    problems: list[AdminCampProblem]
    registrations: list[dict]


# --------------------------------------------------------------------------- #
# 出参拼装
# --------------------------------------------------------------------------- #


def _to_summary(camp: Camp, session: Session) -> CampSummary:
    now = datetime.now(UTC)
    return CampSummary(
        id=camp.id or 0,
        name=camp.name,
        description=camp.description,
        start_at=camp.start_at,
        end_at=camp.end_at,
        started=now >= camp.start_at,
        ended=now >= camp.end_at,
        registration_count=len(
            session.exec(select(CampRegistration).where(CampRegistration.camp_id == camp.id)).all()
        ),
        problem_count=len(
            session.exec(select(CampProblem).where(CampProblem.camp_id == camp.id)).all()
        ),
    )


def _to_detail(data: dict) -> CampDetail:
    return CampDetail(
        id=data["id"],
        name=data["name"],
        description=data["description"],
        start_at=data["start_at"],
        end_at=data["end_at"],
        started=data["started"],
        ended=data["ended"],
        registered=data["registered"],
        registration_count=data["registration_count"],
        problem_count=data["problem_count"],
        platforms=data["platforms"],
        can_register=data["can_register"],
        register_reason=data["register_reason"],
        problems=[CampProblemOut(**p) for p in data["problems"]],
        leaderboard=[
            CampLeaderboardEntry(
                rank=i + 1,
                user_id=u.id or 0,
                display_name=u.display_name,
                student_id=u.student_id,
                enroll_year=u.enroll_year,
                class_label=u.class_label,
                solved_count=count,
                per_problem=per_problem,
            )
            for i, (u, count, per_problem) in enumerate(data["leaderboard"])
        ],
    )


# --------------------------------------------------------------------------- #
# 对外的接口
# --------------------------------------------------------------------------- #


@router.get("/camps", response_model=list[CampSummary])
def list_camps(session: Session = Depends(get_session)) -> list[CampSummary]:
    """所有集训，进行中的在前。"""
    rows = session.exec(select(Camp).order_by(Camp.start_at.desc())).all()
    return [_to_summary(c, session) for c in rows]


@router.get("/camps/{camp_id}", response_model=CampDetail)
def get_camp(
    camp_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(get_optional_user),
) -> CampDetail:
    camp = session.get(Camp, camp_id)
    if camp is None:
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
    camps_mod.unregister(session, camp=camp, user_id=user.id or 0)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 管理员的接口
# --------------------------------------------------------------------------- #


class CampCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    start_at: datetime
    end_at: datetime


class CampUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


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
    return [_to_summary(c, session) for c in rows]


@router.post("/admin/camps", status_code=status.HTTP_201_CREATED, response_model=CampSummary)
def admin_create(
    payload: CampCreate,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin),
) -> CampSummary:
    if payload.end_at <= payload.start_at:
        raise HTTPException(status_code=400, detail="截止时间得晚于开始时间")
    camp = Camp(
        name=payload.name,
        description=payload.description,
        start_at=payload.start_at,
        end_at=payload.end_at,
        created_by=admin.id or 0,
    )
    session.add(camp)
    session.commit()
    session.refresh(camp)
    return _to_summary(camp, session)


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
    session.add(camp)
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
    for p in session.exec(select(CampProblem).where(CampProblem.camp_id == camp_id)).all():
        session.delete(p)
    for r in session.exec(
        select(CampRegistration).where(CampRegistration.camp_id == camp_id)
    ).all():
        session.delete(r)
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
    return AdminCampDetail(
        **_to_summary(camp, session).model_dump(),
        created_by=camp.created_by,
        problems=[
            AdminCampProblem(id=p.id or 0, platform=p.platform, key=p.key, position=p.position)
            for p in problems
        ],
        registrations=[
            {
                "user_id": r.user_id,
                "display_name": users.get(r.user_id).display_name if users.get(r.user_id) else "",
                "registered_at": r.registered_at.isoformat(),
            }
            for r in registrations
        ],
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
    row = CampProblem(
        camp_id=camp_id, platform=payload.platform, key=payload.key, position=payload.position
    )
    session.add(row)
    session.commit()
    return {"ok": True, "id": row.id}


@router.delete(
    "/admin/camps/{camp_id}/problems/{problem_id}", status_code=status.HTTP_204_NO_CONTENT
)
def admin_delete_problem(
    camp_id: int,
    problem_id: int,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> Response:
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
    row = CampRegistration(camp_id=camp_id, user_id=payload.user_id)
    session.add(row)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise HTTPException(status_code=400, detail="这个人已经报过名了") from None
    return {"ok": True}


@router.delete("/admin/camps/{camp_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_remove_member(
    camp_id: int,
    user_id: int,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> Response:
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

    rows = camps_mod.leaderboard(session, camp)
    problems = session.exec(
        select(CampProblem).where(CampProblem.camp_id == camp_id).order_by(CampProblem.position)
    ).all()
    headers = ["学院", "专业", "班级", "姓名", "学号", "洛谷号", "过题数"]
    problem_keys = [f"{p.platform}:{p.key}" for p in problems]
    headers.extend(f"{p.platform}:{p.key}" for p in problems)

    data = []
    for user, count, per_problem in rows:
        luogu = next(
            (a.identifier for a in user.accounts if a.platform == "luogu" and a.verified_at),
            "",
        )
        row = [
            user.college_name or "",
            user.major_name or "",
            user.class_label or "",
            user.display_name,
            user.student_id,
            luogu,
            str(count),
        ]
        row.extend("是" if per_problem.get(k) else "否" for k in problem_keys)
        data.append(row)

    if format == "json":
        import json

        return Response(
            content=json.dumps(
                {
                    "camp": {"id": camp.id, "name": camp.name},
                    "headers": headers,
                    "rows": data,
                },
                ensure_ascii=False,
            ),
            media_type="application/json",
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
