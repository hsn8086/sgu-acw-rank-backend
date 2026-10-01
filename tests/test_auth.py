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


def _members(client) -> list[dict]:
    """取成员列表。接口现在返回分页对象，这里只关心 items。"""
    return client.get("/api/admin/users").json()["items"]


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
    members = _members(root_client)
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
    members = _members(root_client)
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)

    # Admin can write and read it.
    response = root_client.patch(
        f"/api/admin/users/{member['id']}", json={"admin_note": "已核实，学号曾被他人占用"}
    )
    assert response.status_code == 200
    assert response.json()["admin_note"] == "已核实，学号曾被他人占用"

    listed = _members(root_client)
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
    members = _members(root_client)
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

    members = _members(root_client)
    target = next(m for m in members if m["student_id"] == ROOT_STUDENT_ID)
    response = admin_client.patch(f"/api/admin/users/{target['id']}", json={"role": "admin"})
    assert response.status_code == 403


def test_invite_cannot_grant_root(client, root_client):
    response = root_client.post("/api/admin/invites", json={"max_uses": 1, "grants_role": "root"})
    assert response.status_code == 403


def test_root_cannot_be_demoted_or_disabled(client, root_client):
    members = _members(root_client)
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
    members = _members(root_client)
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
    members = _members(root_client)
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
    members = _members(root_client)
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)
    root_client.patch(f"/api/admin/users/{member['id']}", json={"status": "disabled"})

    assert client.get("/api/auth/session").json() == {"user": None}


# --------------------------------------------------------------------------- #
# 成员管理的筛选与 CRUD
# --------------------------------------------------------------------------- #


def _seed_members(client, root_client, count=3):
    """建几个不同年级的成员，供筛选测试用。"""
    invite = _make_invite(root_client, max_uses=count)
    created = []
    for index in range(count):
        response = _register(
            client,
            invite["code"],
            student_id=f"251250230{index + 1:02d}",
            password="password12345",
        )
        created.append(response.json())
    return created


def test_member_list_is_paginated(client, root_client):
    _seed_members(client, root_client, 3)
    body = root_client.get("/api/admin/users?page_size=2").json()

    assert body["total"] == 4  # root + 3
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) == 2
    # 选项由后端给出，前端不写死。
    assert 2025 in body["enroll_years"]
    assert "25" in body["college_codes"]


def test_member_list_second_page(client, root_client):
    _seed_members(client, root_client, 3)
    first = root_client.get("/api/admin/users?page_size=2&page=1").json()
    second = root_client.get("/api/admin/users?page_size=2&page=2").json()

    assert len(second["items"]) == 2
    # 两页不重叠。
    assert {u["id"] for u in first["items"]} & {u["id"] for u in second["items"]} == set()


def test_search_by_student_id_prefix(client, root_client):
    _seed_members(client, root_client, 3)
    body = root_client.get("/api/admin/users?q=25125023001").json()
    assert body["total"] == 1
    assert body["items"][0]["student_id"] == "25125023001"


def test_search_by_name_substring(client, root_client):
    """姓名按包含匹配，搜中间几个字也能找到。"""
    _seed_members(client, root_client, 2)
    body = root_client.get("/api/admin/users?q=测试").json()
    assert body["total"] == 2


def test_filter_by_role_and_status(client, root_client):
    _seed_members(client, root_client, 2)

    roots = root_client.get("/api/admin/users?role=root").json()
    assert roots["total"] == 1
    assert roots["items"][0]["role"] == "root"

    members = root_client.get("/api/admin/users?role=member").json()
    assert members["total"] == 2


def test_filter_by_enroll_year(client, root_client):
    _seed_members(client, root_client, 2)
    body = root_client.get("/api/admin/users?enroll_year=2025").json()
    assert body["total"] == 2


def test_filter_by_account_binding(client, root_client):
    """has_account 用来找出还没绑账号的人。"""
    _seed_members(client, root_client, 2)

    # 都没绑，所以全部落在 false 一侧。
    unbound = root_client.get("/api/admin/users?has_account=false").json()
    assert unbound["total"] == 3
    bound = root_client.get("/api/admin/users?has_account=true").json()
    assert bound["total"] == 0


def test_invalid_role_filter_is_rejected(client, root_client):
    response = root_client.get("/api/admin/users?role=bogus")
    assert response.status_code == 400


def test_page_zero_is_rejected(client, root_client):
    assert root_client.get("/api/admin/users?page=0").status_code == 422


def test_admin_can_create_user_without_invite(client, root_client):
    response = root_client.post(
        "/api/admin/users",
        json={
            "student_id": "23125023001",
            "display_name": "代建成员",
            "password": "createdpassword",
            "role": "member",
            "admin_note": "管理员代建",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["enroll_year"] == 2023
    assert body["college_code"] == "25"
    assert body["admin_note"] == "管理员代建"

    # 建完就能登录。
    fresh = TestClient(client.app)
    assert (
        fresh.post(
            "/api/auth/login",
            json={"student_id": "23125023001", "password": "createdpassword"},
        ).status_code
        == 200
    )


def test_admin_create_rejects_duplicate_student_id(client, root_client):
    payload = {
        "student_id": "23125023001",
        "display_name": "甲",
        "password": "createdpassword",
    }
    assert root_client.post("/api/admin/users", json=payload).status_code == 201
    assert root_client.post("/api/admin/users", json=payload).status_code == 409


def test_admin_create_rejects_bad_student_id(client, root_client):
    response = root_client.post(
        "/api/admin/users",
        json={"student_id": "12345", "display_name": "甲", "password": "createdpassword"},
    )
    assert response.status_code == 400


def test_admin_cannot_create_root(client, root_client):
    response = root_client.post(
        "/api/admin/users",
        json={
            "student_id": "23125023001",
            "display_name": "假root",
            "password": "createdpassword",
            "role": "root",
        },
    )
    assert response.status_code == 403


def test_admin_can_edit_display_name(client, root_client):
    _seed_members(client, root_client, 1)
    member = next(u for u in _members(root_client) if u["role"] == "member")
    response = root_client.patch(
        f"/api/admin/users/{member['id']}", json={"display_name": "新名字"}
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "新名字"


def test_delete_user_removes_accounts_and_sessions(client, root_client):
    _seed_members(client, root_client, 1)
    member = next(u for u in _members(root_client) if u["role"] == "member")

    # 成员当前是登录状态（注册时种了 cookie）。
    assert client.get("/api/auth/me").status_code == 200

    assert root_client.delete(f"/api/admin/users/{member['id']}").status_code == 204
    assert member["student_id"] not in {u["student_id"] for u in _members(root_client)}


def test_cannot_delete_self(client, root_client):
    root = next(u for u in _members(root_client) if u["role"] == "root")
    response = root_client.delete(f"/api/admin/users/{root['id']}")
    assert response.status_code == 400
    assert "自己" in response.json()["detail"]


def test_cannot_delete_root_by_other_admin(client, root_client):
    """管理员不能删 root，即使不是自己。"""
    _seed_members(client, root_client, 1)
    invite = _make_invite(root_client, grants_role="admin")
    _register(client, invite["code"], student_id="25125023099", password="password12345")

    admin_client = TestClient(client.app)
    admin_client.post(
        "/api/auth/login",
        json={"student_id": "25125023099", "password": "password12345"},
    )
    assert admin_client.get("/api/admin/users").status_code == 200

    root = next(u for u in _members(root_client) if u["role"] == "root")
    assert admin_client.delete(f"/api/admin/users/{root['id']}").status_code == 403


# --------------------------------------------------------------------------- #
# 邀请码使用记录与手动刷新额度
# --------------------------------------------------------------------------- #


def test_invite_use_is_recorded(client, root_client):
    """谁用了邀请码要记下来。

    use_count 只知道用了几次，出事时（比如码被转发到群外）光看次数查不出人。
    """
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import InviteUse

    invite = _make_invite(root_client)
    created = _register(client, invite["code"]).json()

    with Session(get_engine()) as db:
        rows = db.exec(select(InviteUse).where(InviteUse.invite_id == invite["id"])).all()
        assert len(rows) == 1
        assert rows[0].user_id == created["id"]


def test_unbinding_account_removes_its_progress(client, root_client):
    """解绑账号要连带删掉练习记录。

    留着的话重新绑同一账号时旧数据会意外复活，看起来像「刚绑上就有几百题」。
    """
    from unittest.mock import patch

    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import DailyProgress
    from app.platforms import Profile

    invite = _make_invite(root_client)
    _register(client, invite["code"])

    with patch(
        "app.routers.accounts.fetch_profile",
        return_value=Profile(platform="codeforces", identifier="tester", signature=""),
    ):
        account = client.post(
            "/api/accounts", json={"platform": "codeforces", "identifier": "tester"}
        ).json()

    # 造一点练习记录。
    with Session(get_engine()) as db:
        db.add(
            DailyProgress(
                account_id=account["id"],
                platform="codeforces",
                date="2026-09-01",
                solved_count=5,
            )
        )
        db.commit()

    assert client.delete(f"/api/accounts/{account['id']}").status_code == 204

    with Session(get_engine()) as db:
        left = db.exec(select(DailyProgress).where(DailyProgress.account_id == account["id"])).all()
        assert left == []


def test_manual_refresh_quota(client, root_client):
    """手动刷新每周三次，用完返回 429。"""
    from unittest.mock import patch

    invite = _make_invite(root_client)
    _register(client, invite["code"])

    with patch(
        "app.routers.accounts.fetch_profile",
        return_value=__import__("app.platforms", fromlist=["Profile"]).Profile(
            platform="codeforces", identifier="tester", signature=""
        ),
    ):
        account = client.post(
            "/api/accounts", json={"platform": "codeforces", "identifier": "tester"}
        ).json()

    # 标记已验证。
    from sqlmodel import Session

    from app.db import get_engine
    from app.models import Account

    with Session(get_engine()) as db:
        row = db.get(Account, account["id"])
        row.verified_at = __import__("app.models", fromlist=["utcnow"]).utcnow()
        db.add(row)
        db.commit()

    quota = client.get("/api/practice/quota").json()
    assert quota["limit"] == 3
    assert quota["remaining"] == 3

    with (
        patch("app.routers.practice.sync_account", return_value=(True, "ok", 0)),
        patch("app.routers.practice.sync_rating", return_value=(True, "ok")),
    ):
        for expected in (2, 1, 0):
            body = client.post("/api/practice/refresh").json()
            assert body["remaining"] == expected

        blocked = client.post("/api/practice/refresh")
        assert blocked.status_code == 429
        assert "用完" in blocked.json()["detail"]


def test_manual_refresh_requires_login(client):
    assert client.post("/api/practice/refresh").status_code == 401
    assert client.get("/api/practice/quota").status_code == 401
