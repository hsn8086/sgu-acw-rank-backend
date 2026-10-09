"""集训的核心逻辑。

一次集训有开始时间和截止时间。开始后题目可见、过题开始算分；截止后
报名截止、算分结束。

「过题」的定义：查这个用户当前的已解决清单里有没有这道集训题，有就算
过——不看什么时候做的，也不记基线。报名时那份快照没有用，因为有人一百
年前做的题，截止时照样算。

报名用户的洛谷账号刷新加速到一小时一次：报名时把它的 ``next_sync``
提前到一小时内，sync 下一轮就会跑到。集训结束后这些账号自然回落到
正常调度。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .models import (
    Account,
    Camp,
    CampProblem,
    CampRegistration,
    ProblemCache,
    User,
)
from .problemlists import solved_keys
from .problems import problem_url


class CampError(Exception):
    """集训业务的错误，带着面向用户的中文消息。"""


def platforms_of(session: Session, camp_id: int) -> set[str]:
    """这份集训涉及哪些平台。"""
    rows = session.exec(
        select(CampProblem.platform).where(CampProblem.camp_id == camp_id).distinct()
    ).all()
    return {r for r in rows}


def verified_platforms(session: Session, user_id: int) -> set[str]:
    """这个用户有已验证账号的平台。"""
    rows = session.exec(
        select(Account.platform).where(
            Account.user_id == user_id, Account.verified_at.isnot(None)
        ).distinct()
    ).all()
    return {r for r in rows}


def can_register(session: Session, *, camp: Camp, user_id: int) -> tuple[bool, str]:
    """这个用户能不能报这份集训。

    返回 (能不能, 原因)。原因是面向用户的提示，能报时是空字符串。
    """
    now = datetime.now(UTC)
    if now >= camp.end_at:
        return False, "已截止"

    needed = platforms_of(session, camp.id or 0)
    have = verified_platforms(session, user_id)

    if not needed:
        return False, "这份集训还没出题"

    missing = needed - have
    if len(missing) == len(needed):
        return False, f"需要以下至少一个平台的已验证账号：{'、'.join(sorted(needed))}"
    if missing:
        return True, f"洛谷的题你做不了：{'、'.join(sorted(missing))} 没有账号"
    return True, ""


def register(session: Session, *, camp: Camp, user_id: int) -> CampRegistration:
    """报名。

    校验账号要求，然后把涉及的账号的 ``next_sync`` 提前——洛谷的集训
    里，报名的洛谷账号下一轮就会被同步。
    """
    ok, reason = can_register(session, camp=camp, user_id=user_id)
    if not ok:
        raise CampError(reason)

    row = CampRegistration(camp_id=camp.id or 0, user_id=user_id)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise CampError("已经报过名了") from None

    # 加速这份集训涉及平台的账号：下一次唤醒立刻刷，并且在集训结束
    # 前保持至少一小时一次。
    #
    # 两件事分开做：
    # * ``solved_fetched_at`` 清成 None —— ``is_due`` 见到 None 返回 True，
    #   下一轮 sync（五分钟内）就会抓它。
    # * ``sync_boost_until`` 设成截止时间 —— 这之前 ``is_due`` 把间隔钳到
    #   一小时，不管活跃度曲线算出多少。集训一结束字段过期，账号自然
    #   回落到正常调度，不需要清理任务。
    # 不动其他平台：报了 CF 的题，洛谷账号不该被催。
    needed = platforms_of(session, camp.id or 0)
    accounts = session.exec(
        select(Account).where(
            Account.user_id == user_id,
            Account.platform.in_(needed),
            Account.verified_at.isnot(None),
        )
    ).all()
    for acc in accounts:
        acc.solved_fetched_at = None
        if acc.sync_boost_until is None or acc.sync_boost_until < camp.end_at:
            acc.sync_boost_until = camp.end_at
        session.add(acc)
    session.commit()

    session.refresh(row)
    return row


def unregister(session: Session, *, camp: Camp, user_id: int) -> None:
    """取消报名。"""
    row = session.exec(
        select(CampRegistration).where(
            CampRegistration.camp_id == camp.id,
            CampRegistration.user_id == user_id,
        )
    ).first()
    if row is None:
        return
    session.delete(row)
    session.commit()


def is_registered(session: Session, *, camp_id: int, user_id: int) -> bool:
    """这个用户报了没有。"""
    return session.exec(
        select(CampRegistration).where(
            CampRegistration.camp_id == camp_id,
            CampRegistration.user_id == user_id,
        )
    ).first() is not None


def leaderboard(session: Session, camp: Camp) -> list[tuple[User, int, dict[str, bool]]]:
    """排行榜：所有报名用户按过题数排序。

    返回 [(用户, 过题数, {题号: 是否通过})]，按过题数降序。
    """
    problems = session.exec(
        select(CampProblem).where(CampProblem.camp_id == camp.id).order_by(CampProblem.position)
    ).all()
    if not problems:
        return []

    registrations = session.exec(
        select(CampRegistration).where(CampRegistration.camp_id == camp.id)
    ).all()
    if not registrations:
        return []

    user_ids = [r.user_id for r in registrations]
    users = {
        u.id: u
        for u in session.exec(select(User).where(User.id.in_(user_ids))).all()
    }

    # 每个用户当前的已解决清单。solved_keys 读的是 SolvedSnapshot——
    # sync 每次跑都会更新这张表，所以这里拿到的是最新状态。
    rows = []
    for reg in registrations:
        user = users.get(reg.user_id)
        if user is None:
            continue
        solved = solved_keys(session, reg.user_id)
        per_problem = {
            f"{p.platform}:{p.key}": (p.platform, p.key) in solved for p in problems
        }
        count = sum(per_problem.values())
        rows.append((user, count, per_problem))

    rows.sort(key=lambda x: x[1], reverse=True)
    return rows


def problem_meta(
    session: Session, problems: list[CampProblem]
) -> dict[tuple[str, str], ProblemCache]:
    """这批题的元信息（名称、难度），一次性查。

    按 ``(platform, key)`` 对，不按 key——同一个题号在不同平台可能各有一条
    cache（比如洛谷的 P11451 和 CF 的 P11451 撞名），只按 key 匹配会拿到
    错误平台那行空缓存。
    """
    keys = {(p.platform, p.key) for p in problems}
    if not keys:
        return {}
    rows = session.exec(
        select(ProblemCache).where(ProblemCache.key.in_({k for _, k in keys}))
    ).all()
    return {(r.platform, r.key): r for r in rows if (r.platform, r.key) in keys}


def detail_for_user(
    session: Session,
    camp: Camp,
    user: User | None,
) -> dict:
    """给某个用户看的详情页数据。

    未登录或未报名：返回基本信息 + 「能不能报名」。
    已报名：返回题目列表 + 排行榜。
    """
    now = datetime.now(UTC)
    started = now >= camp.start_at
    ended = now >= camp.end_at

    user_id = user.id if user else None
    registered = user_id is not None and is_registered(
        session, camp_id=camp.id or 0, user_id=user_id
    )

    problems = session.exec(
        select(CampProblem).where(CampProblem.camp_id == camp.id).order_by(CampProblem.position)
    ).all()

    result = {
        "id": camp.id,
        "name": camp.name,
        "description": camp.description,
        "start_at": camp.start_at,
        "end_at": camp.end_at,
        "started": started,
        "ended": ended,
        "registered": registered,
        "registration_count": len(
            session.exec(
                select(CampRegistration).where(CampRegistration.camp_id == camp.id)
            ).all()
        ),
        "problem_count": len(problems),
        "platforms": sorted(platforms_of(session, camp.id or 0)),
        "can_register": False,
        "register_reason": "",
    }

    if user_id is not None and not registered:
        ok, reason = can_register(session, camp=camp, user_id=user_id)
        result["can_register"] = ok
        result["register_reason"] = reason

    # 题目和排行榜：报名且到开始时间后才给看。管理员在前台跟普通用户一样，
    # 他要预览走 /admin/camps/* 那套独立接口。
    if registered and started:
        meta = problem_meta(session, problems)
        # 这名报名者自己的过题集，一行一题告诉前端「我过没过」。
        my_solved = solved_keys(session, user_id or 0)
        out_problems = []
        for p in problems:
            row = meta.get((p.platform, p.key))
            out_problems.append(
                {
                    "platform": p.platform,
                    "key": p.key,
                    "name": (row.name if row else "") or "",
                    "rating": row.rating if row else None,
                    # 地址现算，不用缓存里的旧值——缓存可能是旧格式。
                    "url": problem_url(p.platform, p.key),
                    "done": (p.platform, p.key) in my_solved,
                }
            )
        result["problems"] = out_problems
        result["leaderboard"] = leaderboard(session, camp)
    else:
        result["problems"] = []
        result["leaderboard"] = []

    return result
