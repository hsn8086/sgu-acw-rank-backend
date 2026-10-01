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
