"""每日练习量排行。

数据来自 ``daily_progress``，那是同步任务算好的，这里只做查询和汇总，
不再实时访问平台。

汇总口径有两个容易踩的点：

* 一个成员在同一平台可以绑多个账号（主号加小号），所以要按人合并，
  而不是按账号各占一行。
* 「今天」按东八区算。CF 和 AtCoder 的时间戳是 UTC，成员心里的今天
  是北京时间。
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_user, require_origin_secret
from ..models import (
    Account,
    DailyProgress,
    Platform,
    RefreshQuota,
    SyncRun,
    User,
    UserStatus,
    utcnow,
)
from ..schemas import (
    DailyPracticeEntry,
    DailyPracticeResponse,
    PracticeDay,
    SyncStatus,
)
from ..sync import current_practice_day, sync_account, sync_rating

router = APIRouter(
    prefix="/practice",
    tags=["practice"],
    dependencies=[Depends(require_origin_secret)],
)


def _recent_dates(days: int) -> list[str]:
    """最近 ``days`` 个练习日（含今天），从早到晚。

    练习日的定义在 app/sync.py：以东八区凌晨 4 点为界，所以凌晨 0 到 3 点
    访问时，「今天」仍然是昨天那个练习日。
    """
    today = date.fromisoformat(current_practice_day())
    return [
        (today - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days - 1, -1, -1)
    ]


@router.get("/rank", response_model=DailyPracticeResponse)
def practice_rank(
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    days: int = Query(default=7, ge=1, le=90, description="统计最近多少天"),
    platform: str | None = Query(default=None, description="只统计某个平台"),
    include_retired: bool = Query(default=False),
) -> DailyPracticeResponse:
    """按最近 N 天的过题总数排行。

    需要登录：榜单把姓名和练习量绑在一起，这正是站点要封起来的原因。
    """
    dates = _recent_dates(days)
    since = dates[0]

    statement = select(DailyProgress).where(DailyProgress.date >= since)
    if platform:
        if platform not in Platform.ALL:
            raise ValueError("不支持的平台")
        statement = statement.where(DailyProgress.platform == platform)
    rows = list(db.exec(statement).all())

    if not rows:
        return DailyPracticeResponse(
            days=days,
            platform=platform,
            dates=dates,
            entries=[],
            generated_at=int(utcnow().timestamp()),
        )

    # 账号 → 人。一次查完，避免逐行查库。
    account_ids = {row.account_id for row in rows}
    accounts = {
        account.id: account
        for account in db.exec(select(Account).where(Account.id.in_(account_ids))).all()  # type: ignore[attr-defined]
    }
    user_ids = {account.user_id for account in accounts.values()}
    users = {
        user.id: user
        for user in db.exec(select(User).where(User.id.in_(user_ids))).all()  # type: ignore[attr-defined]
    }

    # 按人汇总：同一天多个账号的过题数相加。
    per_user: dict[int, dict[str, int]] = {}
    per_user_total: dict[int, int] = {}
    for row in rows:
        account = accounts.get(row.account_id)
        if account is None:
            continue
        user = users.get(account.user_id)
        if user is None:
            continue
        if not include_retired and user.status != UserStatus.ACTIVE:
            continue

        bucket = per_user.setdefault(user.id, {})
        bucket[row.date] = bucket.get(row.date, 0) + row.solved_count
        per_user_total[user.id] = per_user_total.get(user.id, 0) + row.solved_count

    entries: list[DailyPracticeEntry] = []
    for user_id, by_date in per_user.items():
        user = users[user_id]
        entries.append(
            DailyPracticeEntry(
                user_id=user_id,
                display_name=user.display_name,
                student_id=user.student_id,
                enroll_year=user.enroll_year,
                total=per_user_total.get(user_id, 0),
                days=[PracticeDay(date=d, count=by_date.get(d, 0)) for d in dates],
            )
        )

    # 总量降序；并列时看最近一天，还是并列就按年级和姓名稳定排序。
    entries.sort(
        key=lambda e: (
            -e.total,
            -(e.days[-1].count if e.days else 0),
            e.enroll_year,
            e.display_name,
        )
    )
    for index, entry in enumerate(entries, start=1):
        entry.rank = index

    return DailyPracticeResponse(
        days=days,
        platform=platform,
        dates=dates,
        entries=entries,
        generated_at=int(utcnow().timestamp()),
    )


@router.get("/status", response_model=SyncStatus)
def sync_status(
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> SyncStatus:
    """上一次同步的情况，用于在页面上提示数据新旧。"""
    last = db.exec(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)).first()  # type: ignore[attr-defined]
    if last is None:
        return SyncStatus(has_run=False)

    import json

    try:
        errors = json.loads(last.errors_json)
    except json.JSONDecodeError:
        errors = []

    return SyncStatus(
        has_run=True,
        started_at=last.started_at,
        finished_at=last.finished_at,
        account_count=last.account_count,
        ok_count=last.ok_count,
        error_count=last.error_count,
        errors=errors,
    )


# --------------------------------------------------------------------------- #
# 手动刷新
# --------------------------------------------------------------------------- #


def _current_week_start() -> str:
    """本周的周一，YYYY-MM-DD（东八区）。

    按周记账的好处是换周自然重置，不需要额外的清理任务。
    """
    today = date.fromisoformat(current_practice_day())
    return (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")


@router.get("/quota")
def refresh_quota(
    current: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    """本周还剩几次手动刷新机会。"""
    week = _current_week_start()
    row = db.exec(
        select(RefreshQuota).where(
            RefreshQuota.user_id == current.id,
            RefreshQuota.week_start == week,
        )
    ).first()
    used = row.used if row else 0
    return {
        "week_start": week,
        "limit": RefreshQuota.WEEKLY_LIMIT,
        "used": used,
        "remaining": max(0, RefreshQuota.WEEKLY_LIMIT - used),
    }


@router.post("/refresh")
def manual_refresh(
    current: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    """立刻抓一次自己绑定的账号，不等定时任务。

    抓取本身是定时任务在做，成员不需要操心。但有人刚打完一场想立刻看到
    数字变化，所以给一个口子。每周三次，防止有人反复点把平台惹毛。

    额度在真正开始抓之前扣掉，抓失败也不退：失败往往是平台限流，退了额度
    只会让人反复重试，反而更容易被限。
    """
    week = _current_week_start()
    quota = db.exec(
        select(RefreshQuota).where(
            RefreshQuota.user_id == current.id,
            RefreshQuota.week_start == week,
        )
    ).first()

    if quota is None:
        quota = RefreshQuota(user_id=current.id, week_start=week, used=0)
        db.add(quota)
        db.flush()

    if quota.remaining <= 0:
        raise HTTPException(
            status_code=429,
            detail=f"本周的手动刷新次数已用完（{RefreshQuota.WEEKLY_LIMIT} 次），下周一重置",
        )

    quota.used += 1
    db.add(quota)
    db.commit()

    # 只抓自己的账号，且忽略调度周期。
    accounts = list(
        db.exec(
            select(Account).where(
                Account.user_id == current.id,
                Account.verified_at.is_not(None),  # type: ignore[union-attr]
            )
        ).all()
    )

    synced = 0
    added = 0
    failures: list[str] = []
    for account in accounts:
        ok, detail, count = sync_account(db, account, force=True)
        if ok:
            synced += 1
            added += count
        else:
            failures.append(f"{Platform.LABELS.get(account.platform, account.platform)}: {detail}")
        # rating 也顺手刷新，否则「刷新」之后 rating 还是旧的。
        sync_rating(db, account, force=True)
    db.commit()

    return {
        "synced": synced,
        "added": added,
        "failures": failures,
        "remaining": max(0, RefreshQuota.WEEKLY_LIMIT - quota.used),
    }
