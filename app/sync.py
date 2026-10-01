"""同步各平台的解题记录，算出每人每天的过题数。

两种数据形态，两种策略：

* **CF / AtCoder / 牛客** 的记录带精确时间戳。每次同步拉全量，按「每题
  最早一次通过」归到东八区的某一天，直接覆盖当天的计数。重复同步不会
  重复计数。
* **洛谷** 只给「累计通过哪些题」的集合，没有任何时间信息。于是把上一次
  抓到的集合存下来，这次多出来的题记为增量，归到「上次同步至今」这段
  时间里。第一次同步没有基线，只存快照不产生增量——否则会把一个人几年
  的积累全算到今天。

这个模块不依赖 FastAPI，可以单独跑：

    python -m app.sync
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, date, datetime, timedelta, timezone

from sqlmodel import Session, select

from .db import get_engine, init_db, session_scope
from .models import (
    Account,
    DailyProgress,
    Platform,
    SolvedSnapshot,
    SyncRun,
    utcnow,
)
from .platforms import FetchError, SolvedSet, fetch_solved

logger = logging.getLogger("sgu_acw.sync")

#: 统计口径按东八区切日。CF 和 AtCoder 的时间戳是 UTC，成员说的「今天」
#: 是北京时间，直接按 UTC 分会把晚上的提交算到第二天。
CST = timezone(timedelta(hours=8))

#: 一天的起点定在凌晨 4 点而不是 0 点。
#:
#: 比赛经常打到半夜，凌晨一两点交的题在感觉上属于「昨天那一场」。按自然日
#: 切会把这些散到第二天，看上去像是第二天在刷题。定在 4 点，凌晨 0 到 3 点
#: 的记录仍然算前一天。
DAY_BOUNDARY_HOUR = 4

#: 差分时最多回溯多久。如果两次同步隔了特别久（服务停了很久），把增量
#: 平摊到每天会让某一天虚高，不如只记最近这些天，其余忽略。
MAX_BACKFILL_DAYS = 7

#: 保留多少天的历史。榜单只看最近一段时间，多余的删掉控制库大小。
RETENTION_DAYS = 400


def _day_of(epoch: int) -> str:
    """epoch 秒 → 练习日的 YYYY-MM-DD。

    练习日以东八区凌晨 4 点为界：先转成北京时间，再减去 4 小时，取日期。
    于是 0 点到 3 点 59 分的记录归到前一天。
    """
    return practice_day_of(datetime.fromtimestamp(epoch, UTC))


def practice_day_of(moment: datetime) -> str:
    """某个时刻属于哪个练习日。用于把任意时间归到练习日上。"""
    local = moment.astimezone(CST)
    return (local - timedelta(hours=DAY_BOUNDARY_HOUR)).strftime("%Y-%m-%d")


def current_practice_day() -> str:
    """此刻属于哪个练习日。"""
    return practice_day_of(utcnow())


def _upsert_progress(
    db: Session,
    account: Account,
    date: str,
    count: int,
    *,
    replace: bool,
) -> None:
    """写入某账号某天的计数。

    ``replace=True`` 用于有时间戳的平台：那天的真实值就是 count，直接覆盖。
    ``replace=False`` 用于差分出来的增量：在已有值上累加，因为一次同步可能
    只是当天的一部分。
    """
    row = db.exec(
        select(DailyProgress).where(
            DailyProgress.account_id == account.id,
            DailyProgress.date == date,
        )
    ).first()

    if row is None:
        db.add(
            DailyProgress(
                account_id=account.id,
                platform=account.platform,
                date=date,
                solved_count=count,
                updated_at=utcnow(),
            )
        )
        return

    row.solved_count = count if replace else row.solved_count + count
    row.updated_at = utcnow()
    db.add(row)


def _apply_timestamped(
    db: Session,
    account: Account,
    solved: SolvedSet,
    *,
    since_day: str | None = None,
) -> int:
    """有时间戳的平台：按日聚合，覆盖式写入。

    只处理 ``since_day`` 之后的日子，避免每次同步都重写整段历史。
    """
    per_day: Counter[str] = Counter()
    for epoch in solved.problems.values():
        if not epoch:
            continue
        day = _day_of(epoch)
        if since_day and day < since_day:
            continue
        per_day[day] += 1

    for day, count in per_day.items():
        _upsert_progress(db, account, day, count, replace=True)

    return sum(per_day.values())


def _apply_diff(
    db: Session,
    account: Account,
    solved: SolvedSet,
) -> int:
    """没有时间戳的平台：与上次快照比对，多出来的题记为增量。

    返回新增题数。首次同步只建立基线，返回 0。
    """
    snapshot = db.exec(
        select(SolvedSnapshot).where(SolvedSnapshot.account_id == account.id)
    ).first()

    current = set(solved.problems)
    previous = set(json.loads(snapshot.problems_json)) if snapshot else set()

    new_problems = current - previous
    is_first_sync = snapshot is None

    now = utcnow()
    if snapshot is None:
        db.add(
            SolvedSnapshot(
                account_id=account.id,
                problems_json=json.dumps(sorted(current), ensure_ascii=False),
                problem_count=len(current),
                fetched_at=now,
            )
        )
    else:
        snapshot.previous_fetched_at = snapshot.fetched_at
        snapshot.problems_json = json.dumps(sorted(current), ensure_ascii=False)
        snapshot.problem_count = len(current)
        snapshot.fetched_at = now
        db.add(snapshot)

    if is_first_sync or not new_problems:
        # 第一次没有基线，无法判断这些题是什么时候做的。
        return 0

    # 增量归属到「上次同步那天」到今天之间的日子。无法知道具体是哪天做
    # 的，全部记到上次同步那一天，这样总量不会丢，也不会把某天撑得虚高。
    anchor = date.fromisoformat(practice_day_of(snapshot.previous_fetched_at or now))
    today = date.fromisoformat(current_practice_day())
    oldest = today - timedelta(days=MAX_BACKFILL_DAYS)
    if anchor < oldest:
        anchor = oldest

    # 两次同步之间隔了几天，就把增量均摊到这些天，避免全压在同一天。
    span_days = max(1, (today - anchor).days + 1)
    per_day = len(new_problems) // span_days
    remainder = len(new_problems) % span_days

    day = anchor
    for index in range(span_days):
        count = per_day + (1 if index < remainder else 0)
        if count:
            _upsert_progress(
                db,
                account,
                day.strftime("%Y-%m-%d"),
                count,
                replace=False,
            )
        day += timedelta(days=1)

    return len(new_problems)


def sync_account(db: Session, account: Account) -> tuple[bool, str, int]:
    """同步一个账号。返回 ``(是否成功, 说明, 新增题数)``。"""
    try:
        solved = fetch_solved(account.platform, account.identifier)
    except FetchError as exc:
        return False, str(exc), 0

    if account.platform == Platform.LUOGU:
        # 只有洛谷没有时间戳，走差分。
        added = _apply_diff(db, account, solved)
        return True, f"累计 {len(solved.problems)} 题，新增 {added}", added

    # 其余平台（CF / AtCoder / 牛客）都有时间戳。只重算最近一段时间，
    # 够覆盖排行榜展示的窗口，也不必每次重写全部历史。
    since = (date.fromisoformat(current_practice_day()) - timedelta(days=RETENTION_DAYS)).strftime(
        "%Y-%m-%d"
    )
    total = _apply_timestamped(db, account, solved, since_day=since)

    # 存一份快照，便于排查抓取是否正常。
    snapshot = db.exec(
        select(SolvedSnapshot).where(SolvedSnapshot.account_id == account.id)
    ).first()
    payload = json.dumps(sorted(solved.problems), ensure_ascii=False)
    if snapshot is None:
        db.add(
            SolvedSnapshot(
                account_id=account.id,
                problems_json=payload,
                problem_count=len(solved.problems),
                fetched_at=utcnow(),
            )
        )
    else:
        snapshot.problems_json = payload
        snapshot.problem_count = len(solved.problems)
        snapshot.fetched_at = utcnow()
        db.add(snapshot)

    return True, f"累计 {len(solved.problems)} 题", total


def prune_old_progress(db: Session) -> int:
    """删掉超过保留期的记录。返回删除行数。"""
    cutoff = (date.fromisoformat(current_practice_day()) - timedelta(days=RETENTION_DAYS)).strftime(
        "%Y-%m-%d"
    )
    rows = db.exec(select(DailyProgress).where(DailyProgress.date < cutoff)).all()
    for row in rows:
        db.delete(row)
    return len(rows)


def run_sync(*, platforms: list[str] | None = None) -> dict:
    """同步所有已验证账号。返回这次同步的摘要。

    返回普通 dict 而不是 ORM 对象：session 关掉之后 ORM 实例就 detached 了，
    调用方一读属性就报 DetachedInstanceError。
    """
    init_db()

    with session_scope() as db:
        run = SyncRun(started_at=utcnow())
        db.add(run)
        db.flush()
        run_id = run.id

    errors: list[str] = []
    ok = 0
    total = 0
    account_count = 0

    with session_scope() as db:
        statement = select(Account).where(Account.verified_at.is_not(None))  # type: ignore[union-attr]
        if platforms:
            statement = statement.where(Account.platform.in_(platforms))  # type: ignore[attr-defined]
        accounts = list(db.exec(statement).all())
        account_count = len(accounts)

        for account in accounts:
            success, detail, added = sync_account(db, account)
            if success:
                ok += 1
                total += added
                logger.info("synced %s/%s: %s", account.platform, account.identifier, detail)
            else:
                errors.append(f"{account.platform}/{account.identifier}: {detail}")
                logger.warning(
                    "sync failed %s/%s: %s", account.platform, account.identifier, detail
                )

        db.flush()

        run = db.get(SyncRun, run_id)
        if run is not None:
            run.finished_at = utcnow()
            run.account_count = account_count
            run.ok_count = ok
            run.error_count = len(errors)
            run.errors_json = json.dumps(errors, ensure_ascii=False)
            db.add(run)

        pruned = prune_old_progress(db)
        if pruned:
            logger.info("pruned %d old progress rows", pruned)

    logger.info(
        "sync done: %d accounts, %d ok, %d errors, %d new problems",
        account_count,
        ok,
        len(errors),
        total,
    )
    return {
        "account_count": account_count,
        "ok_count": ok,
        "error_count": len(errors),
        "new_problems": total,
        "errors": errors,
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = run_sync()
    print(
        f"同步完成：{result['account_count']} 个账号，"
        f"成功 {result['ok_count']}，失败 {result['error_count']}，"
        f"新增 {result['new_problems']} 题"
    )
    for line in result["errors"]:
        print(f"  ! {line}")


if __name__ == "__main__":
    main()


__all__ = ["run_sync", "sync_account", "get_engine"]
