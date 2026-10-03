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

from datetime import UTC, datetime, timedelta

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

#: 参考代码公布时刻，次日东八区凌晨 4 点——正好是下一次换题。
CODE_PUBLISH_HOUR = 4

#: 支持的语言。键是数据库字段后缀，值是显示名。
LANGUAGES: dict[str, str] = {
    "cpp17": "C++17",
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


def code_publish_at_for(day: str) -> datetime:
    """某一天的参考代码公布时刻：次日东八区 04:00。"""
    date = datetime.strptime(day, "%Y-%m-%d")
    return (date + timedelta(days=1)).replace(tzinfo=CST, hour=CODE_PUBLISH_HOUR)


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
    row.code_cpp17 = code["cpp17"].strip()
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

    由定时任务调用。两种到点：

    - 题解：当天 20:00 之后
    - 代码：次日 04:00 之后

    代码单独记 ``code_published_at``，因为一篇题解的正文和代码是分两次
    对成员可见的——不能用一个布尔量表示。
    """
    moment = now or datetime.now(UTC)
    changed = 0

    drafts = session.exec(select(Editorial).where(Editorial.status == EditorialStatus.DRAFT)).all()
    for row in drafts:
        # 题解到点。
        if row.published_at is None and moment >= publish_at_for(row.day):
            row.published_at = moment
            row.updated_at = moment
            session.add(row)
            changed += 1

        # 代码到点。只有题解已经公布过的才轮到代码，避免某天生成晚了
        # 导致代码比题解先露出来。
        if (
            row.code_published_at is None
            and row.published_at is not None
            and moment >= code_publish_at_for(row.day)
        ):
            row.code_published_at = moment
            row.updated_at = moment
            session.add(row)
            changed += 1

    if changed:
        session.commit()
    return changed


def public_view(row: Editorial, *, now: datetime | None = None) -> dict:
    """给成员看的形态。没到点的部分不返回。

    在服务端裁掉而不是前端隐藏：题解和代码都在响应里的话，随便打开
    开发者工具就能提前看到答案。

    **只看时间，不看 ``published_at``**：

    一开始是「时间戳填了就可见」，但那样得靠定时任务先跑一遍，否则到了
    20:00 而任务还没唤醒，题解就是不可见的——用户看到的是「还没出」，
    而实际上时间早就到了。改成直接拿当前时间和公布时刻比，定时任务就
    只剩「记录什么时候公布的」这一个职责，不再影响可见性。

    ``code_published_at`` 仍然要判：代码必须等题解先公布，避免某天生成
    晚了出现「代码可见但题解还锁着」。
    """
    moment = now or datetime.now(UTC)
    # 先看有没有内容。生成失败或还没生成的，时间到了也不该当成「可读」——
    # 那样用户会看到一个空白页面，比「今晚八点」更糟。
    has_content = row.status in (EditorialStatus.DRAFT, EditorialStatus.PUBLISHED) and bool(
        row.body.strip()
    )
    published = has_content and moment >= publish_at_for(row.day)
    # 代码另外要求题解已经公布过。
    code_ready = (
        published
        and row.published_at is not None
        and moment >= code_publish_at_for(row.day)
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
        "code_available": code_ready,
        "code_publish_at": code_publish_at_for(row.day),
        "code": (
            {
                "cpp17": row.code_cpp17,
                "py3": row.code_py3,
                "java17": row.code_java17,
            }
            if code_ready
            else {}
        ),
    }


def daily_for_day(session: Session, day: str) -> list[DailyProblem]:
    """某一天的每日一题，按难度档排好。"""
    rows = session.exec(select(DailyProblem).where(DailyProblem.day == day)).all()
    order = list(_BAND_ORDER)
    return sorted(rows, key=lambda row: order.index(row.band) if row.band in order else 99)


_BAND_ORDER = ("800-1000", "1100-1300", "1400-1600")


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
    "CODE_PUBLISH_HOUR",
    "LANGUAGES",
    "PUBLISH_HOUR",
    "code_publish_at_for",
    "daily_for_day",
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
