"""每日一题的题解。

**时间线**

    04:00  抽题 → 生成题解（草稿）→ 归档昨天的题解和代码
    20:00  公布今天的题解
    次日 04:00  公布昨天题解的参考代码

生成放在凌晨四点，因为那时候刚抽完题，题目是确定的。公布放在晚上八点，
是给人留一天自己做的时间——题解摆在那里，先看到就没意思了。

**为什么代码比题解晚一天**

题解讲思路，看完还得自己写。代码是最终答案，直接给出来就没得做了。
所以代码等到次日凌晨、这道题不再是「今天的题」时才放出来。

**谁生成**

pi（见 ``tools/generate_editorial``）调 LLM 生成，通过 ``POST /internal/editorials``
写进来。后端不直接调 LLM：生成过程可能要跑代码验证、要重试、要读题面，
那些都需要工具调用能力，塞进 web 进程只会让请求超时。

**生成失败怎么办**

``status`` 记成 failed，``error`` 存原因。前端显示「题解生成失败」而不是
空白，管理员能看到具体原因。不自动重试——失败往往是模型或额度问题，
立刻重试只会重复失败。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, select

from . import daytime
from .models import (
    DailyProblem,
    Editorial,
    EditorialStatus,
    ProblemCache,
    utcnow,
)

CST = daytime.CST

#: 题解公布时刻，东八区晚上 8 点。
PUBLISH_HOUR = 20

#: 支持的语言。键是数据库字段后缀，值是显示名。
LANGUAGES: dict[str, str] = {
    "cpp20": "C++20",
    "py3": "Python 3",
    "java17": "Java 17",
}

#: 题解正文长度上限。太短多半是模型没好好写，太长前端也读不完。
MIN_BODY_CHARS = 200
MAX_BODY_CHARS = 20000
#: 参考代码长度上限。正常题解不会有几千行。
MAX_CODE_CHARS = 20000


def publish_at_for(day: str) -> datetime:
    """某一天的题解公布时刻：当天东八区 20:00。"""
    date = datetime.strptime(day, "%Y-%m-%d")
    return date.replace(tzinfo=CST, hour=PUBLISH_HOUR)


def get_for_problem(session: Session, daily_problem_id: int) -> Editorial | None:
    return session.exec(
        select(Editorial).where(Editorial.daily_problem_id == daily_problem_id)
    ).first()


def get_for_day(session: Session, day: str) -> dict[str, Editorial]:
    """某一天所有题解，按题号索引。"""
    rows = session.exec(select(Editorial).where(Editorial.day == day)).all()
    return {row.key: row for row in rows}


def upsert(
    session: Session,
    *,
    daily_problem_id: int,
    day: str,
    platform: str,
    key: str,
    hints: str,
    body: str,
    code: dict[str, str],
    model: str = "",
) -> Editorial:
    """写入或覆盖一篇题解。

    由 pi 调 ``/internal/editorials`` 触发。同一道题重复提交会覆盖——生成
    可能重跑，重跑的结果应该取代旧的，而不是并存两份。

    校验放在这里而不是路由里：不管从哪个入口进来（pi、管理员手动补、
    以后可能加的定时重试）都该守同一套规则。
    """
    body = body.strip()
    if len(body) < MIN_BODY_CHARS:
        raise ValueError(f"题解正文太短（{len(body)} 字符，至少 {MIN_BODY_CHARS}）")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(f"题解正文太长（{len(body)} 字符，最多 {MAX_BODY_CHARS}）")

    for lang, source in code.items():
        if lang not in LANGUAGES:
            raise ValueError(f"不支持的语言：{lang}")
        if len(source) > MAX_CODE_CHARS:
            raise ValueError(f"{lang} 的代码太长（{len(source)} 字符）")

    missing = [lang for lang in LANGUAGES if not code.get(lang, "").strip()]
    if missing:
        names = "、".join(LANGUAGES[lang] for lang in missing)
        raise ValueError(f"缺少这些语言的代码：{names}")

    row = get_for_problem(session, daily_problem_id)
    if row is None:
        row = Editorial(daily_problem_id=daily_problem_id, day=day, platform=platform, key=key)
        session.add(row)

    row.hints = hints.strip()
    row.body = body
    row.code_cpp20 = code["cpp20"].strip()
    row.code_py3 = code["py3"].strip()
    row.code_java17 = code["java17"].strip()
    row.model = model
    row.status = EditorialStatus.DRAFT
    row.error = ""
    row.generated_at = utcnow()
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def mark_failed(
    session: Session,
    *,
    daily_problem_id: int,
    day: str,
    platform: str,
    key: str,
    error: str,
) -> Editorial:
    """记一次生成失败。

    失败也要落一行，而不是什么都不写：前端靠这一行显示「题解生成失败」，
    否则那道题看起来像是「还没轮到生成」，两种情况该区分开。
    """
    row = get_for_problem(session, daily_problem_id)
    if row is None:
        row = Editorial(daily_problem_id=daily_problem_id, day=day, platform=platform, key=key)
        session.add(row)

    row.status = EditorialStatus.FAILED
    row.error = error[:500]
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def publish_due(session: Session, *, now: datetime | None = None) -> int:
    """把到点的草稿转成已公布。返回改了几篇。

    由定时任务调用。只有一个到点时刻：当天 20:00。

    **正文和代码一起放。** 一开始想让代码晚一天（怕直接看到答案），
    但那要维护两套可见性判据，还多出一条「定时任务恰好漏跑那次，
    代码就永远锁死」的路径。复杂度换来的收益不值，改成一起放。
    """
    moment = now or datetime.now(UTC)
    changed = 0

    drafts = session.exec(select(Editorial).where(Editorial.status == EditorialStatus.DRAFT)).all()
    for row in drafts:
        if moment < publish_at_for(row.day):
            continue
        # status 和 published_at 描述的是同一件事，一起改。
        #
        # 判据只看 status，不看 published_at 是否为空：补跑往日的题解时，
        # 那条记录的 published_at 可能早就被之前的任务填上了（那时题解还
        # 没生成，任务处理不到它），但 status 还是 draft。加上
        # ``published_at is None`` 的话，这条记录就永远停在草稿。
        row.status = EditorialStatus.PUBLISHED
        if row.published_at is None:
            row.published_at = moment
        row.updated_at = moment
        session.add(row)
        changed += 1

    if changed:
        session.commit()
    return changed


def public_view(row: Editorial, *, now: datetime | None = None) -> dict:
    """给成员看的形态。没到公布时间就不返回内容。

    在服务端裁掉而不是前端隐藏：正文和代码都在响应里的话，随便打开
    开发者工具就能提前看到答案。

    **纯时间判断，不看 ``published_at``**：

    一开始是「时间戳填了就可见」，但那样得靠定时任务先跑一遍，否则到了
    20:00 而任务还没唤醒，题解就是不可见的——用户看到「还没出」，而实际
    时间早就到了。改成直接拿当前时间和公布时刻比，定时任务就只剩「记录
    什么时候公布的」这一个职责，不再影响可见性。

    ``published_at`` 仍然保留：管理员可以手动填上它来提前放行，那是
    ``publish_due`` 之外唯一能改变可见性的路径。
    """
    moment = now or datetime.now(UTC)
    # 生成失败或还没生成的，时间到了也不该当成「可读」——那样用户会看到
    # 一个空白页面，比「今晚八点」更糟。
    has_content = row.status in (EditorialStatus.DRAFT, EditorialStatus.PUBLISHED) and bool(
        row.body.strip()
    )
    # 到点，或管理员已经手动放行过。
    published = has_content and (
        moment >= publish_at_for(row.day) or row.published_at is not None
    )

    if not published:
        return {
            "status": row.status,
            "available": False,
            "publish_at": publish_at_for(row.day),
        }

    return {
        "status": row.status,
        "available": True,
        "publish_at": publish_at_for(row.day),
        "hints": [line.strip() for line in row.hints.splitlines() if line.strip()],
        "body": row.body,
        # 代码跟着正文一起可见。字段保留是为了兼容前端的 code_available
        # 判断，恒为 true。
        "code_available": True,
        "code": {
            "cpp20": row.code_cpp20,
            "py3": row.code_py3,
            "java17": row.code_java17,
        },
    }


def problem_meta(session: Session, rows: list[DailyProblem]) -> dict[str, ProblemCache]:
    """批量取题目元信息，避免逐题查库。"""
    if not rows:
        return {}
    keys = [row.key for row in rows]
    found = session.exec(
        select(ProblemCache).where(
            ProblemCache.platform == "codeforces",  # type: ignore[arg-type]
            ProblemCache.key.in_(keys),  # type: ignore[attr-defined]
        )
    ).all()
    return {row.key: row for row in found}


def recent_days(session: Session, *, before: str, limit: int = 30) -> list[str]:
    """有每日一题的往日列表，从新到旧。

    只列真出过题的日期。按自然日往前推会得到一堆空日期——题目落库是
    访问时才发生的，没人访问的那天就没有记录。
    """
    rows = session.exec(
        select(DailyProblem.day)
        .where(DailyProblem.day < before)  # type: ignore[operator]
        .distinct()
        .order_by(DailyProblem.day.desc())  # type: ignore[attr-defined]
        .limit(limit)
    ).all()
    return [day for day in rows if day]


__all__ = [
    "LANGUAGES",
    "PUBLISH_HOUR",
    "get_for_day",
    "get_for_problem",
    "mark_failed",
    "problem_meta",
    "public_view",
    "publish_at_for",
    "publish_due",
    "recent_days",
    "upsert",
]
