"""Tests for the HTTP surface.

Each test gets a fresh SQLite file so nothing leaks between cases. The
``SGU_ACW_*`` environment is set before the app is imported, and the settings
cache is cleared per test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", "")

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c

    db_module.reset_engine()
    get_settings.cache_clear()


def test_healthz(client):
    response = client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_bootstrap_reports_no_users_on_fresh_database(client):
    response = client.get("/api/bootstrap")
    assert response.status_code == 200
    assert response.json() == {"has_users": False, "user_count": 0}


def test_public_config_lists_platforms(client):
    body = client.get("/api/config").json()
    assert [p["value"] for p in body["platforms"]] == [
        "codeforces",
        "atcoder",
        "luogu",
        "nowcoder",
    ]
    assert body["cookie_name"] == "sgu_acw_session"


def test_unknown_api_path_returns_json_404(client):
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_origin_secret_is_enforced_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'guard.db'}")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "s3cret")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", "")

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()

    from app.main import create_app

    with TestClient(create_app()) as c:
        # No header, wrong header, and right header.
        assert c.get("/api/healthz").status_code == 200  # healthz is not guarded
        guarded = c.get("/api/bootstrap")
        assert guarded.status_code == 403
        assert c.get("/api/bootstrap", headers={"X-Origin-Secret": "wrong"}).status_code == 403
        assert c.get("/api/bootstrap", headers={"X-Origin-Secret": "s3cret"}).status_code == 200

    db_module.reset_engine()
    get_settings.cache_clear()


def test_bootstrap_root_creates_account(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'root.db'}")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", "24125023005")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", "rootpass123")

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()

    from app.main import create_app

    with TestClient(create_app()) as c:
        body = c.get("/api/bootstrap").json()
        assert body["has_users"] is True
        assert body["user_count"] == 1

    # Restarting must not create a second account.
    with TestClient(create_app()) as c:
        assert c.get("/api/bootstrap").json()["user_count"] == 1

    db_module.reset_engine()
    get_settings.cache_clear()


def test_bootstrap_root_uses_placeholder_student_id(tmp_path, monkeypatch):
    """不配学号时，root 用十一个 0 这个占位号。

    它不是真实学号，所以不占用任何人的号段；而且学院码 00 非法，别人也
    注册不到它。
    """
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'root2.db'}")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", "rootpass123")
    monkeypatch.delenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", raising=False)

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()

    from app.main import create_app

    with TestClient(create_app()) as c:
        # 用占位学号能登录。
        login = c.post(
            "/api/auth/login",
            json={"student_id": "00000000000", "password": "rootpass123"},
        )
        assert login.status_code == 200, login.text
        assert login.json()["role"] == "root"
        assert login.json()["student_id"] == "00000000000"
        # 年级是占位的 0，前端会显示成空白而不是「0 级」。
        assert login.json()["enroll_year"] == 0

    db_module.reset_engine()
    get_settings.cache_clear()


def test_bootstrap_does_not_create_second_root_after_id_change(tmp_path, monkeypatch):
    """改了 root 的学号之后重启，不该再冒出一个 root。

    原先的幂等性看的是「这个学号存在吗」。一旦有人把 root 的学号从真实学号
    换成占位号（或者反过来），下次重启就会发现「配置里的学号不存在」，于是
    又建一个 root。线上真的这么踩过一次。

    现在先判「有没有 root 角色的人」，有就什么都不做。
    """
    db_path = tmp_path / "rootid.db"
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", "24125023005")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", "rootpass123")

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()

    from app.main import create_app

    # 第一次启动：按配置的学号建 root。
    with TestClient(create_app()) as c:
        assert c.get("/api/bootstrap").json()["user_count"] == 1

    # 有人把 root 的学号改成占位号（模拟运维操作）。
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as db:
        root = db.exec(select(User)).one()
        root.student_id = "00000000000"
        db.add(root)
        db.commit()

    # 再启动：配置里还是旧学号，但已经有 root 了，不该再建。
    with TestClient(create_app()) as c:
        body = c.get("/api/bootstrap").json()
        assert body["user_count"] == 1, "不该出现第二个 root"

    db_module.reset_engine()
    get_settings.cache_clear()


def test_bootstrap_falls_back_when_configured_id_is_taken(tmp_path, monkeypatch):
    """配置里的学号被别人占了时，退回占位号，而不是覆盖那个人。"""
    db_path = tmp_path / "taken.db"
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", "24125023005")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", "rootpass123")

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()

    from sqlmodel import Session, select

    from app.db import get_engine, init_db
    from app.main import bootstrap_root
    from app.models import Role, User, utcnow
    from app.security import hash_password
    from app.student_id import parse_student_id

    init_db()

    # 先占掉那个学号，做成一个普通成员。
    with Session(get_engine()) as db:
        parsed = parse_student_id("24125023005")
        db.add(
            User(
                student_id=parsed.raw,
                password_hash=hash_password("memberpass123"),
                display_name="占用者",
                enroll_year=parsed.enroll_year,
                level=parsed.level,
                college_code=parsed.college_code,
                major_code=parsed.major_code,
                role=Role.MEMBER,
                created_at=utcnow(),
            )
        )
        db.commit()

    bootstrap_root()

    with Session(get_engine()) as db:
        root = db.exec(select(User).where(User.role == Role.ROOT)).one()
        # root 用了占位号，没有把占用者改成 root。
        assert root.student_id == "00000000000"

        occupant = db.exec(select(User).where(User.display_name == "占用者")).one()
        assert occupant.role == Role.MEMBER
        assert occupant.student_id == "24125023005"

    db_module.reset_engine()
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 洛谷的错误说明
# --------------------------------------------------------------------------- #


def test_luogu_error_surfaces_its_own_message():
    """洛谷出错时要把它的原话透传出来。

    直接报「洛谷返回 403」等于没说：用户不知道该去实名认证，只会以为
    是我们这边坏了。

    这类 403 不是反爬——反爬返回的是挑战页面（HTML），这个是结构化 JSON。
    """
    import httpx

    from app.platforms import _luogu_error

    response = httpx.Response(
        403,
        json={
            "instance": "main",
            "status": 403,
            "data": {
                "errorCode": 403,
                "errorMessage": "该用户未通过实名认证",
            },
        },
    )
    assert _luogu_error(response) == "洛谷：该用户未通过实名认证"


def test_luogu_error_falls_back_when_body_is_not_json():
    """返回的不是 JSON（比如真被反爬拦了）时，退回状态码。"""
    import httpx

    from app.platforms import _luogu_error

    html = httpx.Response(403, text="<html>challenge</html>")
    assert _luogu_error(html) == "洛谷返回 403"

    # JSON 里没有 errorMessage 也一样
    empty = httpx.Response(500, json={"status": 500, "data": {}})
    assert _luogu_error(empty) == "洛谷返回 500"
