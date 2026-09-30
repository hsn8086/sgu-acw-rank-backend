"""Tests for registration, login, sessions, invites and admin actions.

Each test runs against a fresh SQLite file. The bootstrap root account is
created via environment variables, which is how the real deployment seeds it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

ROOT_STUDENT_ID = "24125023005"
ROOT_PASSWORD = "rootpassword123"

MEMBER_STUDENT_ID = "25125023001"
MEMBER_PASSWORD = "memberpassword123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Point the app at a fresh database and seed a root account."""
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", ROOT_STUDENT_ID)
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", ROOT_PASSWORD)

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()
    yield
    db_module.reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def client(env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def root_client(env):
    """A client already logged in as root."""
    from app.main import create_app

    with TestClient(create_app()) as c:
        response = c.post(
            "/api/auth/login",
            json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
        )
        assert response.status_code == 200, response.text
        yield c


def _make_invite(client: TestClient, **overrides) -> dict:
    payload = {"max_uses": 1, "ttl_hours": 24, "grants_role": "member", "note": ""}
    payload.update(overrides)
    response = client.post("/api/admin/invites", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _register(
    client: TestClient, code: str, student_id=MEMBER_STUDENT_ID, password=MEMBER_PASSWORD
):
    return client.post(
        "/api/auth/register",
        json={
            "invite_code": code,
            "student_id": student_id,
            "display_name": "测试成员",
            "password": password,
        },
    )


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_register_with_valid_invite(client, root_client):
    invite = _make_invite(root_client)
    response = _register(client, invite["code"])
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["student_id"] == MEMBER_STUDENT_ID
    assert body["display_name"] == "测试成员"
    assert body["role"] == "member"
    # Parsed from the student id, not supplied by the client.
    assert body["enroll_year"] == 2025
    assert body["college_code"] == "25"
    assert body["major_code"] == "02"
    assert "admin_note" not in body
    assert "password_hash" not in body


def test_register_sets_session_cookie(client, root_client):
    invite = _make_invite(root_client)
    response = _register(client, invite["code"])
    assert response.cookies.get("sgu_acw_session")
    # The session cookie must not be readable from JavaScript.
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()


def test_register_rejects_bad_student_id(client, root_client):
    invite = _make_invite(root_client)
    response = _register(client, invite["code"], student_id="12345")
    assert response.status_code == 400
    assert "学号" in response.json()["detail"]


def test_register_rejects_unknown_invite(client):
    response = _register(client, "NOPE1234")
    assert response.status_code == 400


def test_register_rejects_duplicate_student_id(client, root_client):
    invite = _make_invite(root_client, max_uses=5)
    assert _register(client, invite["code"]).status_code == 201
    second = _register(client, invite["code"])
    assert second.status_code == 409


def test_register_does_not_consume_invite_on_duplicate(client, root_client):
    """A failed registration must not burn a use of the invite."""
    invite = _make_invite(root_client, max_uses=2)
    assert _register(client, invite["code"]).status_code == 201
    assert _register(client, invite["code"]).status_code == 409  # duplicate

    listed = root_client.get("/api/admin/invites").json()
    row = next(i for i in listed if i["code"] == invite["code"])
    assert row["use_count"] == 1


def test_register_rejects_short_password(client, root_client):
    invite = _make_invite(root_client)
    response = _register(client, invite["code"], password="short")
    assert response.status_code == 422


def test_invite_max_uses_is_enforced(client, root_client):
    invite = _make_invite(root_client, max_uses=2)
    assert _register(client, invite["code"], student_id="25125023001").status_code == 201
    assert _register(client, invite["code"], student_id="25125023002").status_code == 201
    third = _register(client, invite["code"], student_id="25125023003")
    assert third.status_code == 400
    assert "用尽" in third.json()["detail"]


def test_expired_invite_is_rejected(client, root_client):
    invite = _make_invite(root_client, ttl_hours=1)

    # Reach into the database and backdate the expiry.
    from app.db import get_engine
    from app.models import Invite

    with Session(get_engine()) as db:
        row = db.exec(select(Invite).where(Invite.code == invite["code"])).one()
        row.expires_at = row.expires_at - timedelta(days=2)
        db.add(row)
        db.commit()

    response = _register(client, invite["code"])
    assert response.status_code == 400


def test_revoked_invite_is_rejected(client, root_client):
    invite = _make_invite(root_client)
    assert root_client.delete(f"/api/admin/invites/{invite['id']}").status_code == 204
    assert _register(client, invite["code"]).status_code == 400


# --------------------------------------------------------------------------- #
# Login / logout
# --------------------------------------------------------------------------- #


def test_login_and_me(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])

    fresh = TestClient(client.app)
    assert fresh.get("/api/auth/me").status_code == 401

    login = fresh.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert login.status_code == 200
    me = fresh.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["student_id"] == MEMBER_STUDENT_ID


def test_login_is_case_and_whitespace_insensitive_on_student_id(client):
    response = client.post(
        "/api/auth/login",
        json={"student_id": f"  {ROOT_STUDENT_ID}  ", "password": ROOT_PASSWORD},
    )
    assert response.status_code == 200


def test_login_with_wrong_password(client):
    response = client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": "definitely-wrong"},
    )
    assert response.status_code == 401


def test_login_with_unknown_student_id_gives_same_message(client):
    """The response must not reveal whether a student id exists."""
    unknown = client.post(
        "/api/auth/login",
        json={"student_id": "24125023099", "password": "whatever12345"},
    )
    wrong_pw = client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": "definitely-wrong"},
    )
    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json()["detail"] == wrong_pw.json()["detail"]


def test_logout_invalidates_session(client):
    client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
    )
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_logout_is_idempotent(client):
    assert client.post("/api/auth/logout").status_code == 204
    assert client.post("/api/auth/logout").status_code == 204


def test_disabled_account_cannot_log_in(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    members = root_client.get("/api/admin/users").json()
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)

    patch = root_client.patch(f"/api/admin/users/{member['id']}", json={"status": "disabled"})
    assert patch.status_code == 200

    fresh = TestClient(client.app)
    response = fresh.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Self-service
# --------------------------------------------------------------------------- #


def test_update_own_display_name(client):
    client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
    )
    response = client.patch("/api/auth/me", json={"display_name": "新名字"})
    assert response.status_code == 200
    assert response.json()["display_name"] == "新名字"


def test_member_cannot_change_own_role(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    # The schema has no role field, so it is ignored rather than applied.
    response = client.patch("/api/auth/me", json={"role": "admin"})
    assert response.status_code == 200
    assert response.json()["role"] == "member"


def test_change_password_keeps_caller_logged_in_and_evicts_others(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])

    other = TestClient(client.app)
    other.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert other.get("/api/auth/me").status_code == 200

    changed = client.post(
        "/api/auth/password",
        json={"current_password": MEMBER_PASSWORD, "new_password": "brand-new-password"},
    )
    assert changed.status_code == 204

    # Caller still works, the other session is gone.
    assert client.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401

    # And the new password is what logs in now.
    fresh = TestClient(client.app)
    assert (
        fresh.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": "brand-new-password"},
        ).status_code
        == 200
    )


def test_change_password_requires_current_password(client):
    client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
    )
    response = client.post(
        "/api/auth/password",
        json={"current_password": "wrong", "new_password": "another-password"},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #


def test_member_cannot_reach_admin_endpoints(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/invites").status_code == 403
    assert client.post("/api/admin/invites", json={"max_uses": 1}).status_code == 403


def test_admin_note_is_writable_by_admin_and_hidden_from_members(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    members = root_client.get("/api/admin/users").json()
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)

    # Admin can write and read it.
    response = root_client.patch(
        f"/api/admin/users/{member['id']}", json={"admin_note": "已核实，学号曾被他人占用"}
    )
    assert response.status_code == 200
    assert response.json()["admin_note"] == "已核实，学号曾被他人占用"

    listed = root_client.get("/api/admin/users").json()
    assert (
        next(m for m in listed if m["id"] == member["id"])["admin_note"]
        == "已核实，学号曾被他人占用"
    )

    # The member's own view must not carry the field at all.
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert "admin_note" not in me.json()

    # Nor must the member directory, which other members can read.
    directory = root_client.get("/api/users").json()
    assert all("admin_note" not in entry for entry in directory)


def test_admin_note_can_be_cleared(client, root_client):
    members = root_client.get("/api/admin/users").json()
    root = next(m for m in members if m["student_id"] == ROOT_STUDENT_ID)

    root_client.patch(f"/api/admin/users/{root['id']}", json={"admin_note": "临时备注"})
    cleared = root_client.patch(f"/api/admin/users/{root['id']}", json={"admin_note": ""})
    assert cleared.json()["admin_note"] == ""


def test_admin_cannot_promote_to_admin(client, root_client):
    """Only root hands out administrative roles."""
    invite = _make_invite(root_client, grants_role="admin")
    _register(client, invite["code"])

    # The promoted account is an admin; it must not be able to promote further.
    admin_client = TestClient(client.app)
    admin_client.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert admin_client.get("/api/admin/users").status_code == 200

    members = root_client.get("/api/admin/users").json()
    target = next(m for m in members if m["student_id"] == ROOT_STUDENT_ID)
    response = admin_client.patch(f"/api/admin/users/{target['id']}", json={"role": "admin"})
    assert response.status_code == 403


def test_invite_cannot_grant_root(client, root_client):
    response = root_client.post("/api/admin/invites", json={"max_uses": 1, "grants_role": "root"})
    assert response.status_code == 403


def test_root_cannot_be_demoted_or_disabled(client, root_client):
    members = root_client.get("/api/admin/users").json()
    root = next(m for m in members if m["student_id"] == ROOT_STUDENT_ID)
    assert root["role"] == "root"

    assert (
        root_client.patch(f"/api/admin/users/{root['id']}", json={"role": "member"}).status_code
        == 403
    )
    assert (
        root_client.patch(f"/api/admin/users/{root['id']}", json={"status": "disabled"}).status_code
        == 403
    )


def test_reset_password_returns_temporary_and_invalidates_old(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    members = root_client.get("/api/admin/users").json()
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)

    response = root_client.post(f"/api/admin/users/{member['id']}/reset-password")
    assert response.status_code == 200
    temporary = response.json()["temporary_password"]
    assert len(temporary) >= 8

    # The member's existing session is gone and the old password is dead.
    assert client.get("/api/auth/me").status_code == 401
    fresh = TestClient(client.app)
    assert (
        fresh.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
        ).status_code
        == 401
    )
    assert (
        fresh.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": temporary},
        ).status_code
        == 200
    )


def test_admin_stats(client, root_client):
    body = root_client.get("/api/admin/stats").json()
    assert body["user_count"] == 1
    assert body["admin_count"] == 1
    assert body["usable_invite_count"] == 0


# --------------------------------------------------------------------------- #
# Member directory
# --------------------------------------------------------------------------- #


def test_directory_requires_login(client):
    assert client.get("/api/users").status_code == 401


def test_directory_lists_active_members(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    entries = root_client.get("/api/users").json()
    assert {e["student_id"] for e in entries} == {ROOT_STUDENT_ID, MEMBER_STUDENT_ID}
    assert all("admin_note" not in e for e in entries)


def test_disabled_members_are_hidden_from_the_directory(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    members = root_client.get("/api/admin/users").json()
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)
    root_client.patch(f"/api/admin/users/{member['id']}", json={"status": "disabled"})

    visible = root_client.get("/api/users").json()
    assert MEMBER_STUDENT_ID not in {e["student_id"] for e in visible}


def test_concurrent_registration_cannot_overuse_invite(client, root_client):
    """Many students racing for one slot must not all get in.

    The invite is claimed with a conditional UPDATE so SQLite's single-writer
    rule decides the winner. A check-then-increment in Python would let several
    threads pass the check before any of them incremented.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    invite = _make_invite(root_client, max_uses=1)
    results: list[int | str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        try:
            response = _register(
                client,
                invite["code"],
                student_id=f"251250230{index:02d}",
                password="password12345",
            )
            outcome: int | str = response.status_code
        except Exception as exc:  # pragma: no cover - only on a real bug
            outcome = f"ERR:{type(exc).__name__}"
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(attempt, range(1, 17)))

    assert results.count(201) == 1, f"expected exactly one winner, got {results}"

    listed = root_client.get("/api/admin/invites").json()
    row = next(i for i in listed if i["code"] == invite["code"])
    assert row["use_count"] == 1


def test_session_endpoint_returns_null_when_logged_out(client):
    """页面加载时用它问「我是谁」，未登录也不该是 401。

    /auth/me 对未登录返回 401 是对的，但前端每次加载都调一次，控制台就会
    被 401 刷屏，真正的故障反而看不见。
    """
    response = client.get("/api/auth/session")
    assert response.status_code == 200
    assert response.json() == {"user": None}


def test_session_endpoint_returns_user_when_logged_in(client):
    client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
    )
    body = client.get("/api/auth/session").json()
    assert body["user"]["student_id"] == ROOT_STUDENT_ID
    # 与成员视角一致，不能带出 admin_note。
    assert "admin_note" not in body["user"]


def test_session_endpoint_ignores_a_disabled_account(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    members = root_client.get("/api/admin/users").json()
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)
    root_client.patch(f"/api/admin/users/{member['id']}", json={"status": "disabled"})

    assert client.get("/api/auth/session").json() == {"user": None}
