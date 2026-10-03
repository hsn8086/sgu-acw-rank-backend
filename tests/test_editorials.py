"""题解：时间线、公布裁剪、生成器接口的鉴权与校验。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import daytime, editorials
from app.models import DailyProblem, Editorial, EditorialStatus, ProblemCache, utcnow

ROOT_STUDENT_ID = "00000000000"
ROOT_PASSWORD = "devpassword123"
SECRET = "test-editorial-secret"

#: 够长，能过 MIN_BODY_CHARS。
BODY = "先观察这个式子的结构。\n\n" + "推导过程写在这里。" * 40
CODE = {
    "cpp17": "#include <bits/stdc++.h>\nint main(){return 0;}",
    "py3": "print(0)",
    "java17": "class Main{public static void main(String[] a){}}",
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_EDITORIAL_SECRET", SECRET)
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
    from app.db import get_engine

    with Session(get_engine()) as session:
        yield session


@pytest.fixture()
def client(env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


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


def _seed_problem(
    db: Session,
    *,
    day: str = "2026-03-10",
    key: str = "1000A",
    band: str = "800-1000",
) -> DailyProblem:
    """建一道每日一题，并补上题目元信息。"""
    row = DailyProblem(day=day, band=band, platform="codeforces", key=key)
    db.add(row)
    db.add(
        ProblemCache(
            platform="codeforces",
            key=key,
            name="Test Problem",
            rating=900,
            url=f"https://codeforces.com/problemset/problem/{key}",
        )
    )
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# 时间线
# --------------------------------------------------------------------------- #


def test_publish_and_code_times_are_a_day_apart():
    """题解当晚 8 点，代码次日凌晨 4 点。"""
    publish = editorials.publish_at_for("2026-03-10")
    code = editorials.code_publish_at_for("2026-03-10")

    assert (publish.hour, publish.minute) == (20, 0)
    assert (code.hour, code.minute) == (4, 0)
    assert code.date() == (publish + timedelta(days=1)).date()


def test_draft_hidden_until_publish(db):
    """草稿在公布前对成员不可见，且正文不出现在响应里。"""
    problem = _seed_problem(db)
    editorials.upsert(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        hints="提示一",
        body=BODY,
        code=CODE,
    )

    row = editorials.get_for_problem(db, problem.id or 0)
    assert row is not None

    # 公布时刻之前。
    before = editorials.publish_at_for(problem.day) - timedelta(minutes=1)
    view = editorials.public_view(row, now=before)
    assert view["available"] is False
    # 关键：正文和代码都不能出现在响应里，否则开开发者工具就能提前看到。
    assert "body" not in view
    assert "code" not in view

    # 公布时刻之后。
    after = editorials.publish_at_for(problem.day) + timedelta(minutes=1)
    view = editorials.public_view(row, now=after)
    assert view["available"] is True
    assert view["body"] == BODY


def test_code_hidden_after_editorial_published(db):
    """题解公布后、次日 4 点前，代码仍然不可见。"""
    problem = _seed_problem(db, day="2026-03-10")
    editorials.upsert(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        hints="",
        body=BODY,
        code=CODE,
    )
    row = editorials.get_for_problem(db, problem.id or 0)
    assert row is not None

    # 题解已公布（模拟定时任务已经填过时间戳）。
    row.published_at = editorials.publish_at_for(problem.day)
    db.add(row)
    db.commit()

    at_publish = editorials.publish_at_for(problem.day) + timedelta(minutes=1)
    view = editorials.public_view(row, now=at_publish)
    assert view["available"] is True
    assert view["code_available"] is False
    assert view["code"] == {}

    # 代码到点。
    at_code = editorials.code_publish_at_for(problem.day) + timedelta(minutes=1)
    view = editorials.public_view(row, now=at_code)
    assert view["code_available"] is True
    assert view["code"]["py3"] == CODE["py3"]


def test_publish_due_fills_timestamps(db):
    """到点后定时任务把两个时间戳填上。"""
    problem = _seed_problem(db, day="2026-03-10")
    editorials.upsert(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        hints="",
        body=BODY,
        code=CODE,
    )

    # 还没到点，什么都不改。
    before = editorials.publish_at_for(problem.day) - timedelta(hours=1)
    assert editorials.publish_due(db, now=before) == 0

    # 到公布点。
    at_publish = editorials.publish_at_for(problem.day) + timedelta(minutes=1)
    assert editorials.publish_due(db, now=at_publish) == 1

    # 到代码点。
    at_code = editorials.code_publish_at_for(problem.day) + timedelta(minutes=1)
    assert editorials.publish_due(db, now=at_code) == 1

    # 再跑一次不该重复计数。
    assert editorials.publish_due(db, now=at_code) == 0


def test_code_not_published_before_editorial(db):
    """题解没公布时，代码也不该先露出来。

    否则某天生成晚了，会出现「代码可见但题解还锁着」的怪状态。
    """
    problem = _seed_problem(db, day="2026-03-10")
    editorials.upsert(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        hints="",
        body=BODY,
        code=CODE,
    )

    # 时间已经过了代码点，但题解从未公布过。
    row = editorials.get_for_problem(db, problem.id or 0)
    assert row is not None
    row.published_at = None
    db.add(row)
    db.commit()

    at_code = editorials.code_publish_at_for(problem.day) + timedelta(minutes=1)
    editorials.publish_due(db, now=at_code)

    db.refresh(row)
    assert row.published_at is not None  # 这一轮把它补上了
    assert row.code_published_at is not None


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def test_short_body_rejected(db):
    """太短的正文多半是模型没好好写。"""
    problem = _seed_problem(db)
    with pytest.raises(ValueError, match="太短"):
        editorials.upsert(
            db,
            daily_problem_id=problem.id or 0,
            day=problem.day,
            platform=problem.platform,
            key=problem.key,
            hints="",
            body="太短了",
            code=CODE,
        )


def test_missing_language_rejected(db):
    """三个语言的代码都必须有。"""
    problem = _seed_problem(db)
    with pytest.raises(ValueError, match="缺少"):
        editorials.upsert(
            db,
            daily_problem_id=problem.id or 0,
            day=problem.day,
            platform=problem.platform,
            key=problem.key,
            hints="",
            body=BODY,
            code={"cpp17": CODE["cpp17"]},
        )


def test_unknown_language_rejected(db):
    problem = _seed_problem(db)
    with pytest.raises(ValueError, match="不支持的语言"):
        editorials.upsert(
            db,
            daily_problem_id=problem.id or 0,
            day=problem.day,
            platform=problem.platform,
            key=problem.key,
            hints="",
            body=BODY,
            code={**CODE, "rust": "fn main(){}"},
        )


def test_resubmit_overwrites(db):
    """重跑应该覆盖旧结果，而不是并存两份。"""
    problem = _seed_problem(db)
    first = editorials.upsert(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        hints="旧",
        body=BODY,
        code=CODE,
    )
    second = editorials.upsert(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        hints="新",
        body=BODY + "\n补充。",
        code=CODE,
    )

    assert first.id == second.id
    assert db.exec(select(Editorial)).all().__len__() == 1
    assert second.hints == "新"


def test_failure_recorded(db):
    """失败也要落一行，否则前端分不清「还没生成」和「生成失败」。"""
    problem = _seed_problem(db)
    row = editorials.mark_failed(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        error="模型超时",
    )
    assert row.status == EditorialStatus.FAILED
    assert row.error == "模型超时"

    view = editorials.public_view(row, now=utcnow())
    assert view["available"] is False
    assert view["status"] == EditorialStatus.FAILED


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #


def test_member_endpoint_public(client, db):
    """题解对未登录用户公开。"""
    _seed_problem(db, day=daytime.today())
    response = client.get("/api/daily/editorials")
    assert response.status_code == 200
    body = response.json()
    assert body["day"] == daytime.today()
    assert len(body["problems"]) == 1
    # 还没生成，但接口本身不该报错。
    assert body["problems"][0]["available"] is False


def test_member_endpoint_hides_unpublished(client, db):
    """接口不能把未公布的正文漏出去。

    用一个还没到公布时间的日期。不能用今天——今天可能已经过了 20:00，
    那样题解本就该可见，测不出「裁剪」这个行为。
    """
    problem = _seed_problem(db, day=daytime.today())
    editorials.upsert(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        hints="提示",
        body=BODY,
        code=CODE,
    )

    # 直接调 public_view，把「现在」固定到公布之前。
    row = editorials.get_for_problem(db, problem.id or 0)
    assert row is not None
    before = editorials.publish_at_for(problem.day) - timedelta(hours=1)
    view = editorials.public_view(row, now=before)
    assert view["available"] is False
    assert "body" not in view
    assert "code" not in view

    # 接口层面：今天如果已经过了 20:00，题解应当可见；否则不可见。
    response = client.get("/api/daily/editorials")
    assert response.status_code == 200
    entry = response.json()["problems"][0]
    now = datetime.now(UTC)
    if now >= editorials.publish_at_for(problem.day):
        assert entry["available"] is True
        assert entry["body"] == BODY
    else:
        assert entry["available"] is False
        assert entry["body"] == ""


def test_internal_requires_secret(client):
    """没有密钥时内部接口必须拒绝，且是失败关闭。"""
    assert client.get("/api/internal/editorials/pending").status_code == 403
    assert client.post("/api/internal/editorials", json={}).status_code == 403


def test_internal_disabled_without_secret(monkeypatch, tmp_path):
    """密钥没配时接口返回 503，而不是敞开。"""
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_EDITORIAL_SECRET", "")

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()
    db_module.init_db()

    from app.main import create_app

    with TestClient(create_app()) as c:
        response = c.get("/api/internal/editorials/pending")
        assert response.status_code == 503

    db_module.reset_engine()
    get_settings.cache_clear()


def test_internal_pending_lists_problems(client, db):
    """生成器靠这个接口拿到当天要写的题。"""
    _seed_problem(db, day=daytime.today())
    response = client.get(
        "/api/internal/editorials/pending",
        headers={"X-Editorial-Secret": SECRET},
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["key"] == "1000A"
    assert rows[0]["name"] == "Test Problem"
    assert rows[0]["has_editorial"] is False


def test_internal_submit_writes_editorial(client, db):
    """提交后能读到草稿。"""
    problem = _seed_problem(db, day=daytime.today())
    response = client.post(
        "/api/internal/editorials",
        headers={"X-Editorial-Secret": SECRET},
        json={
            "daily_problem_id": problem.id,
            "hints": "提示一\n提示二",
            "body": BODY,
            "code": CODE,
            "model": "deepseek-flash",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == EditorialStatus.DRAFT

    row = editorials.get_for_problem(db, problem.id or 0)
    assert row is not None
    assert row.model == "deepseek-flash"
    assert row.hints == "提示一\n提示二"


def test_internal_submit_rejects_bad_body(client, db):
    """校验失败要返回 400 并说明原因，好让生成器知道该重跑。"""
    problem = _seed_problem(db, day=daytime.today())
    response = client.post(
        "/api/internal/editorials",
        headers={"X-Editorial-Secret": SECRET},
        json={"daily_problem_id": problem.id, "body": "短", "code": CODE},
    )
    assert response.status_code == 400
    assert "太短" in response.json()["detail"]


def test_internal_fail_endpoint(client, db):
    problem = _seed_problem(db, day=daytime.today())
    response = client.post(
        "/api/internal/editorials/fail",
        headers={"X-Editorial-Secret": SECRET},
        json={"daily_problem_id": problem.id, "error": "模型超时"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == EditorialStatus.FAILED


def test_publish_requires_admin(client, db):
    """手动放行只有管理员能做。"""
    problem = _seed_problem(db, day=daytime.today())
    row = editorials.upsert(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        hints="",
        body=BODY,
        code=CODE,
    )
    assert client.post(f"/api/daily/editorials/{row.id}/publish").status_code == 401


def test_recent_days_lists_past_days(client, db):
    """往日列表只列真出过题的日期。"""
    _seed_problem(db, day="2026-03-01", key="1001A")
    _seed_problem(db, day="2026-03-02", key="1002A")

    response = client.get("/api/daily/editorials/recent")
    assert response.status_code == 200
    days = [row["day"] for row in response.json()]
    assert "2026-03-02" in days
    assert "2026-03-01" in days
    # 从新到旧。
    assert days.index("2026-03-02") < days.index("2026-03-01")


def test_redraw_removes_old_editorials(db):
    """重抽要能把旧题解一起删掉，否则会撞外键约束。

    题解的 daily_problem_id 指向 daily_problems，重抽时先删题再删题解
    会直接 500。这个测试盯的就是那个顺序。
    """
    from app import daily as daily_mod

    day = daily_mod.workshop_today()
    problem = _seed_problem(db, day=day, key="1000A")
    editorials.upsert(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        hints="",
        body=BODY,
        code=CODE,
    )
    assert db.exec(select(Editorial)).all()

    # 重抽。候选池在测试库里是空的，这里只关心「不报错且旧题解被清掉」。
    daily_mod.ensure_daily(db, day=day, force=True, salt="test")

    assert db.exec(select(Editorial)).all() == []


def test_hints_split_into_lines(db):
    """hint 按行拆开，前端可以逐条显示。"""
    problem = _seed_problem(db)
    editorials.upsert(
        db,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        hints="第一条\n\n第二条\n  第三条  ",
        body=BODY,
        code=CODE,
    )
    row = editorials.get_for_problem(db, problem.id or 0)
    assert row is not None
    view = editorials.public_view(
        row, now=editorials.publish_at_for(problem.day) + timedelta(hours=1)
    )
    assert view["hints"] == ["第一条", "第二条", "第三条"]
