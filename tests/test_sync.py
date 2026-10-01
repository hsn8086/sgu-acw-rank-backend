"""每日练习量统计的测试。

重点覆盖两件容易错的事：

* 一个人在同一平台绑了多个账号时，两个账号的题数要相加而不是互相覆盖。
* 重复同步不能重复计数。CF/AtCoder 的提交带时间戳，按日覆盖写；洛谷靠
  快照差分，第一次只建基线不产生增量。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.db import get_engine
from app.models import Account, DailyProgress, Platform, SolvedSnapshot, User, utcnow
from app.platforms import SolvedSet
from app.sync import CST, _apply_diff, _apply_timestamped, sync_account


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'sync.db'}")
    monkeypatch.setenv("SGU_ACW_SCRYPT_N", "1024")

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()

    from app.db import init_db

    init_db()
    yield
    db_module.reset_engine()
    get_settings.cache_clear()


def _make_user(db: Session, student_id: str, name: str) -> User:
    user = User(
        student_id=student_id,
        password_hash="x",
        display_name=name,
        enroll_year=2024,
        level=1,
        college_code="25",
        major_code="02",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_account(
    db: Session, user: User, identifier: str, platform: str = Platform.CODEFORCES
) -> Account:
    account = Account(
        user_id=user.id,
        platform=platform,
        identifier=identifier,
        verified_at=utcnow(),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _epoch(days_ago: int, hour: int = 12) -> int:
    """距今 ``days_ago`` 个练习日、北京时间 ``hour`` 点的 epoch 秒。

    以练习日为基准而不是自然日，这样在凌晨 0 到 3 点跑测试时，
    ``_epoch(0)`` 和 ``_day(0)`` 仍然指向同一天。
    """
    from datetime import date

    from app.sync import current_practice_day

    day = date.fromisoformat(current_practice_day()) - timedelta(days=days_ago)
    moment = datetime(day.year, day.month, day.day, hour, tzinfo=CST)
    return int(moment.timestamp())


def _day(days_ago: int) -> str:
    """距今 ``days_ago`` 个练习日的日期。"""
    from datetime import date

    from app.sync import current_practice_day

    today = date.fromisoformat(current_practice_day())
    return (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _progress(db: Session, account: Account, date: str) -> int:
    row = db.exec(
        select(DailyProgress).where(
            DailyProgress.account_id == account.id, DailyProgress.date == date
        )
    ).first()
    return row.solved_count if row else 0


def test_timestamped_writes_per_day(env):
    """有时间的平台按天归档。"""
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")

        solved = SolvedSet(
            platform=Platform.CODEFORCES,
            identifier="alpha",
            problems={"a": _epoch(1), "b": _epoch(1), "c": _epoch(2)},
        )
        _apply_timestamped(db, account, solved)
        db.commit()

        assert _progress(db, account, _day(1)) == 2
        assert _progress(db, account, _day(2)) == 1


def test_timestamped_is_idempotent(env):
    """同一份数据同步两次，计数不变。"""
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")
        solved = SolvedSet(
            platform=Platform.CODEFORCES,
            identifier="alpha",
            problems={"a": _epoch(1), "b": _epoch(1)},
        )

        _apply_timestamped(db, account, solved)
        db.commit()
        first = _progress(db, account, _day(1))

        _apply_timestamped(db, account, solved)
        db.commit()
        assert _progress(db, account, _day(1)) == first == 2


def test_two_accounts_same_user_do_not_overwrite(env):
    """同一个人两个账号，各记各的，互不覆盖。

    这是按账号而非按人建键的原因：按人建键的话，后同步的账号会把先前的
    覆盖掉，总量凭空少一半。
    """
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        main = _make_account(db, user, "main")
        alt = _make_account(db, user, "alt")

        _apply_timestamped(
            db, main, SolvedSet(Platform.CODEFORCES, "main", {"a": _epoch(1), "b": _epoch(1)})
        )
        _apply_timestamped(db, alt, SolvedSet(Platform.CODEFORCES, "alt", {"c": _epoch(1)}))
        db.commit()

        assert _progress(db, main, _day(1)) == 2
        assert _progress(db, alt, _day(1)) == 1

        # 按人汇总时应当是 3。
        rows = db.exec(select(DailyProgress).where(DailyProgress.date == _day(1))).all()
        assert sum(r.solved_count for r in rows) == 3


def test_diff_first_sync_creates_baseline_only(env):
    """洛谷首次同步只建基线，不产生增量。

    否则会把一个人几年的积累全算到今天。
    """
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "1425874", platform=Platform.LUOGU)

        added = _apply_diff(
            db,
            account,
            SolvedSet(Platform.LUOGU, "1425874", {"P1001": 0, "P1002": 0, "P1003": 0}),
        )
        db.commit()

        assert added == 0
        rows = db.exec(select(DailyProgress).where(DailyProgress.account_id == account.id)).all()
        assert rows == []

        snapshot = db.exec(
            select(SolvedSnapshot).where(SolvedSnapshot.account_id == account.id)
        ).one()
        assert snapshot.problem_count == 3


def test_diff_second_sync_counts_new_problems(env):
    """第二次同步多出来的题记为增量。"""
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "1425874", platform=Platform.LUOGU)

        _apply_diff(db, account, SolvedSet(Platform.LUOGU, "1425874", {"P1": 0, "P2": 0}))
        db.commit()

        added = _apply_diff(
            db, account, SolvedSet(Platform.LUOGU, "1425874", {"P1": 0, "P2": 0, "P3": 0, "P4": 0})
        )
        db.commit()

        assert added == 2
        total = sum(
            r.solved_count
            for r in db.exec(
                select(DailyProgress).where(DailyProgress.account_id == account.id)
            ).all()
        )
        assert total == 2


def test_diff_with_no_change_adds_nothing(env):
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "1425874", platform=Platform.LUOGU)

        _apply_diff(db, account, SolvedSet(Platform.LUOGU, "1425874", {"P1": 0}))
        db.commit()
        added = _apply_diff(db, account, SolvedSet(Platform.LUOGU, "1425874", {"P1": 0}))
        db.commit()

        assert added == 0


def test_sync_account_reports_fetch_failure(env, monkeypatch):
    """抓取失败时返回失败而不是抛异常，让同步继续跑完其他账号。"""
    from app.platforms import FetchError

    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "ghost")

        def boom(platform, identifier):
            raise FetchError("平台上没有这个用户", not_found=True)

        monkeypatch.setattr("app.sync.fetch_solved", boom)
        ok, detail, added = sync_account(db, account)

        assert ok is False
        assert "没有这个用户" in detail
        assert added == 0


def test_day_boundary_uses_east_eight(env):
    """按东八区切日，且凌晨 4 点才换天。

    北京时间今天 01:00 属于**昨天**那个练习日：比赛打到半夜，凌晨交的题
    在感觉上还是昨天那一场。
    """
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")

        solved = SolvedSet(Platform.CODEFORCES, "alpha", {"a": _epoch(0, hour=1)})
        _apply_timestamped(db, account, solved)
        db.commit()

        # 归到昨天，不是今天。
        assert _progress(db, account, _day(1)) == 1
        assert _progress(db, account, _day(0)) == 0


def test_day_boundary_at_four_am(env):
    """4 点前后分属不同的练习日。"""
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")

        # 今天 03:30 仍是昨天，04:30 就是今天。
        _apply_timestamped(
            db, account, SolvedSet(Platform.CODEFORCES, "alpha", {"a": _epoch(0, hour=3)})
        )
        _apply_timestamped(
            db, account, SolvedSet(Platform.CODEFORCES, "alpha", {"b": _epoch(0, hour=5)})
        )
        db.commit()

        assert _progress(db, account, _day(1)) == 1  # 03:30 → 昨天
        assert _progress(db, account, _day(0)) == 1  # 05:00 → 今天


def test_practice_day_helper():
    """练习日的定义：东八区减 4 小时取日期。"""
    from datetime import datetime

    from app.sync import CST, practice_day_of

    cases = [
        (datetime(2026, 10, 1, 0, 30, tzinfo=CST), "2026-09-30"),
        (datetime(2026, 10, 1, 3, 59, tzinfo=CST), "2026-09-30"),
        (datetime(2026, 10, 1, 4, 0, tzinfo=CST), "2026-10-01"),
        (datetime(2026, 10, 1, 23, 30, tzinfo=CST), "2026-10-01"),
    ]
    for moment, expected in cases:
        assert practice_day_of(moment) == expected, moment


# --------------------------------------------------------------------------- #
# 牛客
# --------------------------------------------------------------------------- #


def test_nowcoder_row_parsing():
    """牛客练习页的表格解析。

    这页是服务端直出的 HTML，字段位置固定：运行ID、题目、判题结果、得分、
    运行时间、内存、代码长度、语言、提交时间。解析错了会静默少算题数，
    所以拿一段真实结构的片段钉住。
    """
    from app.platforms import _nowcoder_rows, _parse_nowcoder_time

    page = """
    <table><tbody>
    <tr><td>84566970</td><td><a href="/acm/problem/318505">恋爱模拟器</a></td>
        <td>答案正确</td><td>100</td><td>112</td><td>26168</td><td>319</td>
        <td>PyPy3</td><td>2026-08-19 23:47:35</td></tr>
    <tr><td>84566959</td><td><a href="/acm/problem/318505">恋爱模拟器</a></td>
        <td>答案错误</td><td>0</td><td>121</td><td>25532</td><td>262</td>
        <td>PyPy3</td><td>2026-08-19 23:44:01</td></tr>
    <tr><th>表头不是数据行</th></tr>
    </tbody></table>
    """
    rows = _nowcoder_rows(page)
    assert len(rows) == 2
    assert rows[0] == ("84566970", "恋爱模拟器", "答案正确", "2026-08-19 23:47:35")
    assert rows[1][2] == "答案错误"

    # 页面上的时间是北京时间，不带时区标记。
    epoch = _parse_nowcoder_time("2026-08-19 23:47:35")
    assert epoch is not None
    assert _day_of_epoch(epoch) == "2026-08-19"


def _day_of_epoch(epoch: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).astimezone(CST).strftime("%Y-%m-%d")


def test_nowcoder_only_counts_accepted(env, monkeypatch):
    """只有「答案正确」计入，错误提交不算过题。"""
    from app.platforms import (
        _nowcoder_rows,  # noqa: F401
        fetch_solved_nowcoder,
    )

    page = """
    <table><tbody>
    <tr><td>1</td><td>题目甲</td><td>答案正确</td><td>100</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-19 10:00:00</td></tr>
    <tr><td>2</td><td>题目乙</td><td>答案错误</td><td>0</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-19 11:00:00</td></tr>
    </tbody></table>
    """

    class FakeResponse:
        status_code = 200
        text = page

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

    solved = fetch_solved_nowcoder(FakeClient(), "825377486")
    assert set(solved.problems) == {"nowcoder:题目甲"}


def test_nowcoder_dedupes_by_earliest(env):
    """同一题多次通过时只记最早一次。"""
    from app.platforms import fetch_solved_nowcoder

    page = """
    <table><tbody>
    <tr><td>1</td><td>题目甲</td><td>答案正确</td><td>100</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-20 10:00:00</td></tr>
    <tr><td>2</td><td>题目甲</td><td>答案正确</td><td>100</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-19 10:00:00</td></tr>
    </tbody></table>
    """

    class FakeResponse:
        status_code = 200
        text = page

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

    solved = fetch_solved_nowcoder(FakeClient(), "825377486")
    assert _day_of_epoch(solved.problems["nowcoder:题目甲"]) == "2026-08-19"
