"""每日一题：候选池筛选、抽签确定性、不重复、权限。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import daily

ROOT_STUDENT_ID = "00000000000"
ROOT_PASSWORD = "devpassword123"
MEMBER_STUDENT_ID = "24125023001"
MEMBER_PASSWORD = "xzxze123456"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", ROOT_STUDENT_ID)
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", ROOT_PASSWORD)

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()
    db_module.init_db()
    yield
    db_module.reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def db(env):
    """直接操作数据库的测试用这个。

    ``init_db`` 已经在 ``env`` 里调过了，这里只是把 session 包一层，
    省得每个测试都写 ``Session(get_engine())``。
    """
    from app.db import get_engine

    with Session(get_engine()) as session:
        yield session


@pytest.fixture()
def root_client(env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        response = c.post(
            "/api/auth/login",
            json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
        )
        assert response.status_code == 200, response.text
        yield c


@pytest.fixture()
def member_client(env, root_client):
    response = root_client.post(
        "/api/admin/users",
        json={
            "student_id": MEMBER_STUDENT_ID,
            "password": MEMBER_PASSWORD,
            "display_name": "谢泽栩",
        },
    )
    assert response.status_code in (200, 201), response.text

    from app.main import create_app

    with TestClient(create_app()) as c:
        login = c.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
        )
        assert login.status_code == 200, login.text
        yield c


def seed_problems(session: Session, entries: list[dict]) -> None:
    """往题目缓存里塞测试数据。

    字段名和 ``fetch_all_codeforces`` 写进去的一致。
    """
    from app.models import ProblemCache

    for entry in entries:
        session.add(
            ProblemCache(
                platform=entry.get("platform", "codeforces"),
                key=entry["key"],
                name=entry.get("name", f"Problem {entry['key']}"),
                rating=entry.get("rating"),
                tags_json="[]",
                url=entry.get("url", f"https://codeforces.com/problemset/problem/{entry['key']}"),
                solved_count=entry.get("solved_count"),
            )
        )
    session.commit()


#: 造数据用的比赛 id 起点。
#:
#: 必须落在 2024 年之前（id < 1918），否则会被「只要老题」的过滤条件筛掉。
#: 三个档位各占一段，互不重叠，方便按档位断言。
_BASE_CONTEST_ID = {"800-1000": 1000, "1100-1300": 1100, "1400-1600": 1200}


def make_user(session: Session, student_id: str, display_name: str = "测试成员") -> int:
    """建一个成员，返回 id。

    签到表有指向 ``users`` 的外键，不能拿裸数字当 user_id。
    """
    from app.models import User
    from app.student_id import parse_student_id

    parsed = parse_student_id(student_id)
    user = User(
        student_id=parsed.raw,
        display_name=display_name,
        password_hash="x",
        enroll_year=parsed.enroll_year,
        level=parsed.level,
        college_code=parsed.college_code,
        major_code=parsed.major_code,
        class_no=parsed.class_no,
        role="member",
        status="active",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    return user.id


def make_pool(band: str, count: int, solved: int = 5000) -> list[dict]:
    """造一批指定档位的题。

    题号形如 ``1000A``、``1001A``……比赛 id 都在 2024 年之前。

    注意别把 ``band`` 当数字前缀用——``f"{1000}{i:02d}A"`` 会拼出
    ``100000A``，比赛 id 变成 100000，那是 2024 年之后的题，会被过滤掉。
    """
    base = _BASE_CONTEST_ID[band]
    rating = daily.BANDS[band][0]
    return [
        {
            "key": f"{base + index}A",
            "rating": rating,
            "solved_count": solved,
        }
        for index in range(count)
    ]


# --------------------------------------------------------------------------- #
# 候选池
# --------------------------------------------------------------------------- #


def test_candidates_skip_problems_without_enough_solvers(env):
    """过题人数不够的题不进池子。

    rating 是拿参与者的成绩估的，做的人太少就不准。这种题抽出来，
    写着 1400 实际做起来像 1800。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            [
                {"key": "1000A", "rating": 900, "solved_count": 100},
                {"key": "1000B", "rating": 900, "solved_count": 2500},
            ],
        )
        pools = daily.load_candidates(session)

    keys = [c.key for c in pools["800-1000"]]
    assert keys == ["1000B"]


def test_candidates_skip_problems_from_2024_onwards(env):
    """2024 年及以后的题不进池子。

    分界线是 contest id：2024-01-01 之前最大 1917，之后最小 1918。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            [
                {"key": "1917A", "rating": 900, "solved_count": 5000},
                {"key": "1918A", "rating": 900, "solved_count": 5000},
                {"key": "2000A", "rating": 900, "solved_count": 5000},
            ],
        )
        pools = daily.load_candidates(session)

    assert [c.key for c in pools["800-1000"]] == ["1917A"]


def test_candidates_skip_problems_without_rating(env):
    """没 rating 的题不进池子。分不了档就没法抽。"""
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            [
                {"key": "1000A", "rating": None, "solved_count": 5000},
                {"key": "1000B", "rating": 900, "solved_count": 5000},
            ],
        )
        pools = daily.load_candidates(session)

    assert [c.key for c in pools["800-1000"]] == ["1000B"]


def test_candidates_are_split_into_the_right_bands(env):
    """每道题只进一个档位，边界值是闭区间。

    档位之间有空档（1001~1099、1301~1399），落在里面的题不参与抽签——
    CF 的 rating 是 100 一档的，实际不会出现这些值，但逻辑上不该把它们
    塞进相邻档位。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            [
                {"key": "1000A", "rating": 800, "solved_count": 5000},
                {"key": "1000B", "rating": 1000, "solved_count": 5000},
                {"key": "1000C", "rating": 1001, "solved_count": 5000},  # 空档
                {"key": "1000D", "rating": 1100, "solved_count": 5000},
                {"key": "1000E", "rating": 1300, "solved_count": 5000},
                {"key": "1000F", "rating": 1301, "solved_count": 5000},  # 空档
                {"key": "1000G", "rating": 1400, "solved_count": 5000},
                {"key": "1000H", "rating": 1600, "solved_count": 5000},
                {"key": "1000I", "rating": 1601, "solved_count": 5000},  # 超出上限
            ],
        )
        pools = daily.load_candidates(session)

    assert [c.key for c in pools["800-1000"]] == ["1000A", "1000B"]
    assert [c.key for c in pools["1100-1300"]] == ["1000D", "1000E"]
    assert [c.key for c in pools["1400-1600"]] == ["1000G", "1000H"]


def test_candidates_are_sorted_numerically_not_lexically(env):
    """排序按数字，不是按字符串。

    字符串排的话 ``"1000A" < "999B"``，池子顺序会乱，抽签结果就不稳定。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            [
                {"key": "999B", "rating": 900, "solved_count": 5000},
                {"key": "1000A", "rating": 900, "solved_count": 5000},
            ],
        )
        pools = daily.load_candidates(session)

    assert [c.key for c in pools["800-1000"]] == ["999B", "1000A"]


# --------------------------------------------------------------------------- #
# 抽签
# --------------------------------------------------------------------------- #


def test_pick_is_deterministic_for_the_same_day(env):
    """同一天抽多次，结果一样。

    没有这个性质的话，同一个人刷新页面就会看到不同的题。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(session, make_pool("800-1000", 20))
        pools = daily.load_candidates(session)
        pool = pools["800-1000"]

        first = daily.pick_for_band(pool, day="2026-01-01", band="800-1000")
        for _ in range(5):
            again = daily.pick_for_band(pool, day="2026-01-01", band="800-1000")
            assert again.key == first.key


def test_same_day_gives_everyone_the_same_problems(root_client, env):
    """全站统一：同一天，不同的人拿到同一套题。

    抽签只由日期决定，和「谁在看」无关。否则每个人看到的题不一样，
    就没法在群里讨论今天这道题了。
    """
    from app.db import get_engine
    from app.main import create_app

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )

    admin_view = [p["key"] for p in root_client.get("/api/daily").json()["problems"]]

    # 换一个完全没登录的客户端，等价于另一个人
    with TestClient(create_app()) as other:
        other_view = [p["key"] for p in other.get("/api/daily").json()["problems"]]

    assert admin_view == other_view


def test_problems_may_repeat_across_days(env):
    """题目可以重复出现。

    池子里只有三道题时，连着几天必然重复——这是允许的，不做「抽过
    不再抽」的账。真要保证长期不重复，池子会被抽空，还得处理重置，
    而重复本身没什么坏处。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 1) + make_pool("1100-1300", 1) + make_pool("1400-1600", 1),
        )

        first = [r.key for r in daily.ensure_daily(session, day="2026-03-01")]
        second = [r.key for r in daily.ensure_daily(session, day="2026-03-02")]

    assert first == second, "池子里只有一道题，两天应该抽到同一道"


def test_pick_differs_across_days(env):
    """不同天抽出的题不同（池子够大的时候）。

    只有一道题时必然相同，所以这里给足候选。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(session, make_pool("800-1000", 50))
        pools = daily.load_candidates(session)
        pool = pools["800-1000"]

        picks = {
            daily.pick_for_band(pool, day=f"2026-01-{day:02d}", band="800-1000").key
            for day in range(1, 11)
        }

    assert len(picks) > 1, "十天抽出同一道题，说明种子没起作用"


def test_pick_returns_none_for_empty_pool(env):
    """池子空的时候返回 None，不要抛异常。

    数据库还没同步过 CF 题库时就是这个状态，接口要能正常响应，
    前端显示「还没准备好」而不是 500。
    """
    from app import daily

    assert daily.pick_for_band([], day="2026-01-01", band="800-1000") is None


# --------------------------------------------------------------------------- #
# 落库与幂等
# --------------------------------------------------------------------------- #


def test_ensure_daily_persists_and_is_idempotent(env):
    """第一次调用落库，之后读同一份记录。"""
    from app import daily
    from app.db import get_engine
    from app.models import DailyProblem

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )

        first = daily.ensure_daily(session, day="2026-03-01")
        assert len(first) == 3

        second = daily.ensure_daily(session, day="2026-03-01")
        assert [r.key for r in first] == [r.key for r in second]

        stored = session.exec(select(DailyProblem)).all()
        assert len(stored) == 3


def test_ensure_daily_covers_all_three_bands(env):
    """每天三道，三个档位各一道。"""
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )
        rows = daily.ensure_daily(session, day="2026-03-01")

    assert [r.band for r in rows] == list(daily.BAND_ORDER)


def test_ensure_daily_force_redraws(env):
    """force=True 时重抽，换掉原来的题。

    必须带 salt——不带的话种子不变，算出来还是同一套。
    """
    from app import daily
    from app.db import get_engine
    from app.models import DailyProblem

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 30) + make_pool("1100-1300", 30) + make_pool("1400-1600", 30),
        )

        before = [r.key for r in daily.ensure_daily(session, day="2026-03-01")]
        after = [
            r.key
            for r in daily.ensure_daily(
                session, day="2026-03-01", force=True, salt="redraw-1"
            )
        ]

        assert before != after
        # 重抽后库里仍然只有三道，不会累积。
        assert len(session.exec(select(DailyProblem)).all()) == 3


def test_ensure_daily_tolerates_missing_bands(env):
    """某个档位没有候选时，其他档位照常出题。

    题库刚同步一半，或者某一档恰好被抽空，都不该让整个功能挂掉。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(session, make_pool("800-1000", 5))  # 只有 800-1000
        rows = daily.ensure_daily(session, day="2026-03-01")

    assert [r.band for r in rows] == ["800-1000"]


# --------------------------------------------------------------------------- #
# 时区
# --------------------------------------------------------------------------- #


def test_workshop_today_uses_china_time(env):
    """日期按东八区算。

    按 UTC 算的话，晚上八点之后就算成第二天，用户会觉得「今天的题提前换了」。
    """
    from datetime import datetime

    from app import daily, daytime

    # UTC 2026-03-01 20:00 = 东八区 2026-03-02 04:00（刚好换日）
    assert daily.workshop_today(datetime(2026, 3, 1, 20, 0, tzinfo=UTC)) == "2026-03-02"
    # UTC 2026-03-01 15:59 = 东八区 2026-03-01 23:59
    assert daily.workshop_today(datetime(2026, 3, 1, 15, 59, tzinfo=UTC)) == "2026-03-01"
    assert daytime.CST is not None


def test_workshop_today_starts_at_four_am(env):
    """出题日以凌晨 4 点为界，与练习数据同一套口径。

    比赛打到半夜，凌晨一两点还在做题的人不该看到题目突然换了。
    """
    from datetime import datetime

    from app import daily, daytime

    cases = [
        (0, 30, "2026-09-30"),
        (3, 59, "2026-09-30"),
        (4, 0, "2026-10-01"),
        (23, 30, "2026-10-01"),
    ]
    for hour, minute, expected in cases:
        moment = datetime(2026, 10, 1, hour, minute, tzinfo=daytime.CST)
        assert daily.workshop_today(moment) == expected, f"{hour}:{minute:02d} 归到了错误的日期"


def test_workshop_today_matches_practice_day(env):
    """每日一题的工作日和练习数据的练习日是同一天。

    两边各写各的就会出现「练习数据算昨天、每日一题算今天」的错位。
    """
    from datetime import datetime, timedelta

    from app import daily, daytime
    from app.sync import practice_day_of

    for offset in range(0, 48):
        moment = datetime(2026, 10, 1, 0, 0, tzinfo=daytime.CST) + timedelta(hours=offset)
        assert daily.workshop_today(moment) == practice_day_of(moment), (
            f"{moment.isoformat()} 两边算出的日期不一致"
        )


def test_next_reset_is_four_am(env):
    """换题时刻是东八区凌晨 4 点。"""
    from datetime import datetime

    from app import daily, daytime

    # 还没到 4 点：就是今天的 4 点
    before = datetime(2026, 3, 1, 3, 0, tzinfo=daytime.CST)
    assert daily.next_reset_at(before).isoformat() == "2026-03-01T04:00:00+08:00"

    # 已经过了 4 点：是明天 4 点
    after = datetime(2026, 3, 1, 12, 0, tzinfo=daytime.CST)
    assert daily.next_reset_at(after).isoformat() == "2026-03-02T04:00:00+08:00"

    # 正好 4 点：也算过了，推到明天
    exact = datetime(2026, 3, 1, 4, 0, tzinfo=daytime.CST)
    assert daily.next_reset_at(exact).isoformat() == "2026-03-02T04:00:00+08:00"


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #


def test_api_returns_three_problems(root_client, env):
    """接口返回三道题，带档位和题面链接。"""
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )

    response = root_client.get("/api/daily")
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["problems"]) == 3
    assert [p["band"] for p in body["problems"]] == ["800-1000", "1100-1300", "1400-1600"]
    for problem in body["problems"]:
        assert problem["url"].startswith("https://codeforces.com/")
        assert problem["rating"] is not None


def test_api_is_idempotent_within_a_day(root_client, env):
    """同一天调两次拿到同一套题。"""
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )

    first = root_client.get("/api/daily").json()
    second = root_client.get("/api/daily").json()
    assert [p["key"] for p in first["problems"]] == [p["key"] for p in second["problems"]]


def test_api_works_without_login(env):
    """没登录也能看每日一题。

    这是公开内容，不需要账号。只是没有「做过了」的标记。
    """
    from app.db import get_engine
    from app.main import create_app

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )

    with TestClient(create_app()) as c:
        response = c.get("/api/daily")

    assert response.status_code == 200
    for problem in response.json()["problems"]:
        assert problem["solved"] is False


def test_api_rejects_bad_date(root_client, env):
    """日期格式不对返回 400，不是 500。"""
    response = root_client.get("/api/daily", params={"day": "2026/03/01"})
    assert response.status_code == 400


def test_api_accepts_explicit_date(root_client, env):
    """可以查指定日期。"""
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )

    response = root_client.get("/api/daily", params={"day": "2026-03-01"})
    assert response.status_code == 200
    assert response.json()["day"] == "2026-03-01"


def test_redraw_requires_admin(member_client, env):
    """普通成员不能重抽。"""
    response = member_client.post("/api/daily/redraw")
    assert response.status_code == 403


def test_redraw_changes_the_problems(root_client, env):
    """管理员重抽会换题。"""
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 30) + make_pool("1100-1300", 30) + make_pool("1400-1600", 30),
        )

    before = [p["key"] for p in root_client.get("/api/daily").json()["problems"]]
    after = [p["key"] for p in root_client.post("/api/daily/redraw").json()["problems"]]

    assert before != after


def test_api_marks_solved_problems(root_client, env):
    """过了的题标出来。"""
    from app.db import get_engine
    from app.models import Account, SolvedSnapshot, User

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )

    picked = root_client.get("/api/daily").json()["problems"]
    target = picked[0]["key"]

    with Session(get_engine()) as session:
        root = session.exec(select(User).where(User.student_id == ROOT_STUDENT_ID)).one()
        account = Account(
            user_id=root.id,
            platform="codeforces",
            identifier="test_handle",
            verified_at=datetime.now(UTC),
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        session.add(
            SolvedSnapshot(
                account_id=account.id,
                problems_json=f'["{target}"]',
                problem_count=1,
            )
        )
        session.commit()

    after = root_client.get("/api/daily").json()["problems"]
    marked = {p["key"]: p["solved"] for p in after}
    assert marked[target] is True
    assert any(value is False for value in marked.values())


# --------------------------------------------------------------------------- #
# 完成人数
# --------------------------------------------------------------------------- #


def test_members_solved_counts_distinct_members(root_client, env):
    """完成人数数的是人，不是提交次数。

    一个人在同一道题上提交多次只算一个。
    """
    from app.db import get_engine
    from app.models import Account, SolvedSnapshot, User

    with Session(get_engine()) as session:
        seed_problems(session, make_pool("800-1000", 3))

    picked = root_client.get("/api/daily").json()["problems"][0]["key"]

    with Session(get_engine()) as session:
        root = session.exec(select(User).where(User.student_id == ROOT_STUDENT_ID)).one()
        account = Account(
            user_id=root.id,
            platform="codeforces",
            identifier="solver_a",
            verified_at=datetime.now(UTC),
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        session.add(
            SolvedSnapshot(
                account_id=account.id,
                problems_json=f'["{picked}"]',
                problem_count=1,
            )
        )
        session.commit()

    problems = root_client.get("/api/daily").json()["problems"]
    counts = {p["key"]: p["members_solved"] for p in problems}
    assert counts[picked] == 1
    assert sum(counts.values()) == 1


def test_members_solved_merges_multiple_accounts(root_client, env):
    """同一个人的主号小号取并集，不重复计人。

    一个人绑了 CF 主号和小号，两边都过了同一道题，人数还是 1。
    """
    from app.db import get_engine
    from app.models import Account, SolvedSnapshot, User

    with Session(get_engine()) as session:
        seed_problems(session, make_pool("800-1000", 3))

    picked = root_client.get("/api/daily").json()["problems"][0]["key"]

    with Session(get_engine()) as session:
        root = session.exec(select(User).where(User.student_id == ROOT_STUDENT_ID)).one()
        for handle in ("main_acc", "alt_acc"):
            account = Account(
                user_id=root.id,
                platform="codeforces",
                identifier=handle,
                verified_at=datetime.now(UTC),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            session.add(
                SolvedSnapshot(
                    account_id=account.id,
                    problems_json=f'["{picked}"]',
                    problem_count=1,
                )
            )
        session.commit()

    problems = root_client.get("/api/daily").json()["problems"]
    counts = {p["key"]: p["members_solved"] for p in problems}
    assert counts[picked] == 1, "两个账号属于同一个人，应该只算一个"


def test_members_solved_is_zero_without_snapshots(root_client, env):
    """没人做过时是 0，不是 null。"""
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(session, make_pool("800-1000", 3))

    for problem in root_client.get("/api/daily").json()["problems"]:
        assert problem["members_solved"] == 0


# --------------------------------------------------------------------------- #
# 签到
# --------------------------------------------------------------------------- #


def test_record_solves_marks_a_checkin(env):
    """做对当天的一道题就记一次签到。"""
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 3) + make_pool("1100-1300", 3) + make_pool("1400-1600", 3),
        )
        rows = daily.ensure_daily(session, day="2026-03-01")
        target = rows[0]
        uid = make_user(session, "24125023099")

        recorded = daily.record_solves(
            session,
            user_id=uid,
            account_id=None,
            day="2026-03-01",
            solved_keys={(target.platform, target.key)},
        )

    assert recorded is True


def test_record_solves_ignores_other_days_problems(env):
    """做对「不是今天的」题不算今天的签到。

    「每日」一题的意思就是当天的。这里特意构造一道不在今天的题，而不是
    拿昨天的题来试——池子小的时候两天可能抽到同一道（题目允许重复），
    那样断言就失去意义了。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 30) + make_pool("1100-1300", 30) + make_pool("1400-1600", 30),
        )
        today = daily.ensure_daily(session, day="2026-03-02")
        today_keys = {(row.platform, row.key) for row in today}

        # 找一道今天没出的题
        pools = daily.load_candidates(session)
        other = next(
            candidate
            for candidate in pools["800-1000"]
            if ("codeforces", candidate.key) not in today_keys
        )
        uid = make_user(session, "24125023099")

        recorded = daily.record_solves(
            session,
            user_id=uid,
            account_id=None,
            day="2026-03-02",
            solved_keys={("codeforces", other.key)},
        )

    assert recorded is False


def test_record_solves_is_idempotent(env):
    """同一天重复记只算一次。同步一天跑很多次。"""
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 3) + make_pool("1100-1300", 3) + make_pool("1400-1600", 3),
        )
        rows = daily.ensure_daily(session, day="2026-03-01")
        keys = {(rows[0].platform, rows[0].key)}
        uid = make_user(session, "24125023099")

        assert daily.record_solves(
            session, user_id=uid, account_id=None, day="2026-03-01", solved_keys=keys
        )
        assert not daily.record_solves(
            session, user_id=uid, account_id=None, day="2026-03-01", solved_keys=keys
        )
        board = daily.checkin_leaderboard(session)
        assert len(board) == 1
        assert board[0].user_id == uid
        assert board[0].days == 1


def test_record_solves_without_daily_problems_is_noop(env):
    """那天没抽题时静默跳过，不抛异常。"""
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(session, make_pool("800-1000", 3))
        uid = make_user(session, "24125023099")
        recorded = daily.record_solves(
            session,
            user_id=uid,
            account_id=None,
            day="2026-03-01",
            solved_keys={("codeforces", "1000A")},
        )

    assert recorded is False


def test_checkin_streak_counts_consecutive_days(env):
    """连续签到天数。"""
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )
        uid = make_user(session, "24125023099")
        for day in ("2026-03-01", "2026-03-02", "2026-03-03"):
            rows = daily.ensure_daily(session, day=day)
            daily.record_solves(
                session,
                user_id=uid,
                account_id=None,
                day=day,
                solved_keys={(rows[0].platform, rows[0].key)},
            )

        assert daily.checkin_streak(session, user_id=uid, today="2026-03-03") == 3
        # 隔了一天就断了
        assert daily.checkin_streak(session, user_id=uid, today="2026-03-05") == 0


def test_checkin_streak_survives_today_not_yet_signed(env):
    """今天还没签不算断。

    一天还没过完，此时把连续数归零会让人以为白签了。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )
        uid = make_user(session, "24125023099")
        for day in ("2026-03-01", "2026-03-02"):
            rows = daily.ensure_daily(session, day=day)
            daily.record_solves(
                session,
                user_id=uid,
                account_id=None,
                day=day,
                solved_keys={(rows[0].platform, rows[0].key)},
            )

        assert daily.checkin_streak(session, user_id=uid, today="2026-03-03") == 2


def test_checkin_leaderboard_orders_by_streak(env):
    """排行榜按连续天数降序，不是累计天数。

    累计天数堆起来就不会掉，签得久的人哪怕很久没来也一直挂在前面，
    榜单就定死了。按连续排，今天没签明天就掉下去。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )
        veteran = make_user(session, "24125023001", "签得久但断了")
        steady = make_user(session, "24125023002", "稳定连续")

        # 「签得久但断了」：累计 5 天，但最近三天没签，连续为 0
        for day in ("2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"):
            rows = daily.ensure_daily(session, day=day)
            daily.record_solves(
                session,
                user_id=veteran,
                account_id=None,
                day=day,
                solved_keys={(rows[0].platform, rows[0].key)},
            )

        # 「稳定连续」：只签了 3 天，但一直连到今天
        for day in ("2026-03-06", "2026-03-07", "2026-03-08"):
            rows = daily.ensure_daily(session, day=day)
            daily.record_solves(
                session,
                user_id=steady,
                account_id=None,
                day=day,
                solved_keys={(rows[0].platform, rows[0].key)},
            )

        board = daily.checkin_leaderboard(session, today="2026-03-08")

    # 连续 3 的排在累计 5 但连续 0 的前面
    assert board[0].user_id == steady
    assert board[0].streak == 3
    assert board[0].days == 3
    assert board[1].user_id == veteran
    assert board[1].days == 5
    assert board[1].streak == 0


def test_checkin_leaderboard_breaks_ties_by_total(env):
    """连续天数相同就比累计天数。"""
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )
        few = make_user(session, "24125023001", "签得少")
        many = make_user(session, "24125023002", "签得多")

        # 两人都连续签到 3 天（3-06 ~ 3-08），但「签得多」之前还签过两天
        for day in ("2026-03-01", "2026-03-02"):
            rows = daily.ensure_daily(session, day=day)
            daily.record_solves(
                session,
                user_id=many,
                account_id=None,
                day=day,
                solved_keys={(rows[0].platform, rows[0].key)},
            )
        for day in ("2026-03-06", "2026-03-07", "2026-03-08"):
            rows = daily.ensure_daily(session, day=day)
            keys = {(rows[0].platform, rows[0].key)}
            daily.record_solves(session, user_id=few, account_id=None, day=day, solved_keys=keys)
            daily.record_solves(session, user_id=many, account_id=None, day=day, solved_keys=keys)

        board = daily.checkin_leaderboard(session, today="2026-03-08")

    assert board[0].streak == board[1].streak == 3
    assert board[0].user_id == many, "连续相同时累计多的应该排前面"
    assert board[0].days == 5
    assert board[1].days == 3


def test_checkin_leaderboard_order_is_stable(env):
    """连续和累计都一样时，按 user_id 排，顺序不随机。

    不加这个兜底的话，两次请求的顺序可能不一样，看着像在跳。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )
        ids = [make_user(session, f"2412502300{i}", f"第{i}个") for i in range(1, 4)]
        for day in ("2026-03-07", "2026-03-08"):
            rows = daily.ensure_daily(session, day=day)
            keys = {(rows[0].platform, rows[0].key)}
            for uid in ids:
                daily.record_solves(
                    session, user_id=uid, account_id=None, day=day, solved_keys=keys
                )

        first = [s.user_id for s in daily.checkin_leaderboard(session, today="2026-03-08")]
        second = [s.user_id for s in daily.checkin_leaderboard(session, today="2026-03-08")]

    assert first == second
    assert first == sorted(ids)


def test_checkin_board_lists_only_participants(root_client, env):
    """一次没签过的人不上榜。"""
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 3) + make_pool("1100-1300", 3) + make_pool("1400-1600", 3),
        )

    board = root_client.get("/api/daily/checkin").json()
    assert board["entries"] == []


def test_checkin_board_renders_entries(root_client, env):
    """榜单里有人时，每一行的字段都齐。

    只断言「空榜单返回空数组」是不够的——那样循环体一次都不执行，
    里面写错（比如把属性当方法调）也发现不了。这个测试专门把数据
    铺进去，让序列化路径真正跑一遍。
    """
    from app import daily
    from app.db import get_engine

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 10) + make_pool("1100-1300", 10) + make_pool("1400-1600", 10),
        )
        uid = make_user(session, "24125023099", "有数据的人")
        for day in ("2026-03-01", "2026-03-02"):
            rows = daily.ensure_daily(session, day=day)
            daily.record_solves(
                session,
                user_id=uid,
                account_id=None,
                day=day,
                solved_keys={(rows[0].platform, rows[0].key)},
            )

    response = root_client.get("/api/daily/checkin")
    assert response.status_code == 200, response.text
    board = response.json()

    assert board["total_members"] == 1
    entry = board["entries"][0]
    assert entry["rank"] == 1
    assert entry["display_name"] == "有数据的人"
    assert entry["days"] == 2
    # 班级简称是拼出来的，能取到就说明 property 被正确读取了
    assert entry["class_label"] == "信工软工 3 班"
    assert entry["enroll_year"] == 2024


def test_api_reports_checkin_state(root_client, env):
    """接口带回签到状态。"""
    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as session:
        seed_problems(
            session,
            make_pool("800-1000", 3) + make_pool("1100-1300", 3) + make_pool("1400-1600", 3),
        )

    body = root_client.get("/api/daily").json()
    assert body["checkin_days"] == 0
    assert body["checkin_streak"] == 0
    assert body["checked_in_today"] is False
    assert body["checked_in_today_names"] == []

    # 手动记一次签到，接口应该反映出来
    from app import daily

    with Session(get_engine()) as session:
        root = session.exec(select(User).where(User.student_id == ROOT_STUDENT_ID)).one()
        rows = daily.ensure_daily(session, day=body["day"])
        daily.record_solves(
            session,
            user_id=root.id,
            account_id=None,
            day=body["day"],
            solved_keys={(rows[0].platform, rows[0].key)},
        )

    after = root_client.get("/api/daily").json()
    assert after["checkin_days"] == 1
    assert after["checked_in_today"] is True
    assert after["checked_in_today_names"] == ["root"]


# --------------------------------------------------------------------------- #
# 题号规范化
# --------------------------------------------------------------------------- #


def test_normalize_nowcoder_numeric_key(env):
    """牛客的裸数字补成竞赛站。

    数字在牛客上是竞赛站的题目 id（主站的题是 uuid，不是数字）。
    不补的话，题单里存的是裸数字，过题记录里是带前缀的 key，两边
    永远比对不上，完成度一直显示 0。
    """
    from app.models import Platform
    from app.problems import normalize_key

    assert normalize_key(Platform.NOWCODER, "233234") == "nowcoder:contest:233234"
    assert normalize_key(Platform.NOWCODER, "  233234  ") == "nowcoder:contest:233234"


def test_normalize_nowcoder_keeps_existing_prefix(env):
    """已经带前缀的原样返回，不能叠一层。"""
    from app.models import Platform
    from app.problems import normalize_key

    assert (
        normalize_key(Platform.NOWCODER, "nowcoder:contest:233234")
        == "nowcoder:contest:233234"
    )
    assert (
        normalize_key(Platform.NOWCODER, "nowcoder:main:abc-123")
        == "nowcoder:main:abc-123"
    )


def test_normalize_nowcoder_uuid_goes_to_main(env):
    """带字母的当作主站 uuid。

    主站的题面路径是 questionTerminal/{uuid}，走竞赛站会 404。
    """
    from app.models import Platform
    from app.problems import normalize_key

    assert normalize_key(Platform.NOWCODER, "abc123def") == "nowcoder:main:abc123def"


def test_normalize_leaves_other_platforms_alone(env):
    """别的平台不转换。"""
    from app.models import Platform
    from app.problems import normalize_key

    for platform, key in [
        (Platform.CODEFORCES, "1004A"),
        (Platform.LUOGU, "P1001"),
        (Platform.ATCODER, "dp_h"),
    ]:
        assert normalize_key(platform, key) == key


def test_nowcoder_url_accepts_all_key_forms(env):
    """牛客题号的三种写法都要拼得出地址。

    * ``nowcoder:contest:233234`` —— 内部标准格式
    * ``contest:233234`` —— 去掉平台名
    * ``233234`` —— 迁移前的裸数字

    标准格式这条曾经是坏的：拼链接那边按 ``split(":", 1)`` 解析，会把
    ``nowcoder`` 当成来源，两个分支都不匹配，全部落到搜索页。也就是说
    所有牛客题目的链接都点不到题面。
    """
    from app.models import Platform
    from app.problems import problem_url

    for key in ("nowcoder:contest:233234", "contest:233234", "233234"):
        assert (
            problem_url(Platform.NOWCODER, key)
            == "https://ac.nowcoder.com/acm/problem/233234"
        ), f"{key} 没拼对"

    for key in ("nowcoder:main:abc-123", "main:abc-123"):
        assert (
            problem_url(Platform.NOWCODER, key)
            == "https://www.nowcoder.com/questionTerminal/abc-123"
        ), f"{key} 没拼对"


def test_parse_nowcoder_key_handles_all_forms(env):
    """解析出来的来源和 id 要对，拼链接和抓题目都靠它。"""
    from app.problems import parse_nowcoder_key

    assert parse_nowcoder_key("233234") == ("contest", "233234")
    assert parse_nowcoder_key("nowcoder:contest:233234") == ("contest", "233234")
    assert parse_nowcoder_key("contest:233234") == ("contest", "233234")
    assert parse_nowcoder_key("nowcoder:main:abc-123") == ("main", "abc-123")
    # 早期的 ``nowcoder:题名`` 格式
    assert parse_nowcoder_key("nowcoder:A+B Problem") == ("legacy", "A+B Problem")


# --------------------------------------------------------------------------- #
# AtCoder 地址
# --------------------------------------------------------------------------- #


def test_atcoder_url_handles_contest_without_digits(env):
    """比赛名里没有数字也要能拼。

    ``dp`` 是 AtCoder 的经典 DP 专题赛，名字里一个数字都没有。原来那个
    「字母 + 数字」的模式匹配不上，落到兜底的竞赛列表页，抓回来的是
    列表页 HTML，取不到标题。
    """
    from app.models import Platform
    from app.problems import problem_url

    assert (
        problem_url(Platform.ATCODER, "dp_h")
        == "https://atcoder.jp/contests/dp/tasks/dp_h"
    )


def test_atcoder_url_handles_digits_at_the_end(env):
    """``typical90`` 这种数字在末尾的也要支持。"""
    from app.models import Platform
    from app.problems import problem_url

    assert (
        problem_url(Platform.ATCODER, "typical90_a")
        == "https://atcoder.jp/contests/typical90/tasks/typical90_a"
    )


def test_atcoder_url_handles_numeric_problem_suffix(env):
    """``abc001_1``：题号部分是数字而不是字母。"""
    from app.models import Platform
    from app.problems import problem_url

    assert (
        problem_url(Platform.ATCODER, "abc001_1")
        == "https://atcoder.jp/contests/abc001/tasks/abc001_1"
    )


def test_atcoder_url_handles_compound_contest_name(env):
    """``joi2020yo1a``：比赛名里数字和字母交错。"""
    from app.models import Platform
    from app.problems import problem_url

    assert (
        problem_url(Platform.ATCODER, "joi2020yo1a_a")
        == "https://atcoder.jp/contests/joi2020yo1a/tasks/joi2020yo1a_a"
    )


def test_atcoder_url_without_underscore_falls_back(env):
    """格式不对时退回竞赛列表，不要拼出坏地址。"""
    from app.models import Platform
    from app.problems import problem_url

    assert problem_url(Platform.ATCODER, "abc300") == "https://atcoder.jp/contests/"


def test_problem_entry_url_is_computed_not_cached(env):
    """题单里的题面地址现算，不用缓存里那份。

    缓存没有过期时间，抓取逻辑改了之后旧结果会一直留着。牛客踩过：
    地址拼接修好了，缓存里存的还是修复前拼出来的搜索页链接，用户点的
    还是坏链接。
    """
    from app.models import ProblemCache, ProblemListItem
    from app.problemlists import to_entry

    item = ProblemListItem(list_id=1, platform="nowcoder", key="nowcoder:contest:233234")
    stale = ProblemCache(
        platform="nowcoder",
        key="nowcoder:contest:233234",
        name="233234",
        url="https://www.nowcoder.com/exam/oj",  # 修复前缓存下来的坏地址
    )

    entry = to_entry(item, stale, None)

    assert entry.url == "https://ac.nowcoder.com/acm/problem/233234"
    # 名字仍然来自缓存——那个没法现算
    assert entry.name == "233234"
