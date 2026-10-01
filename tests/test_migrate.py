"""迁移脚本的测试。

关注两件事：幂等（跑两次不会出错），以及新列有正确的默认值（不能是 NULL）。
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def old_db(tmp_path):
    """造一个「旧版本」的库：accounts 表缺了新加的那些列。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
          id INTEGER NOT NULL PRIMARY KEY,
          student_id VARCHAR(16) NOT NULL UNIQUE,
          password_hash VARCHAR NOT NULL,
          display_name VARCHAR(64) NOT NULL,
          enroll_year INTEGER NOT NULL,
          level INTEGER NOT NULL,
          college_code VARCHAR(4) NOT NULL,
          major_code VARCHAR(4) NOT NULL,
          role VARCHAR(16) NOT NULL,
          status VARCHAR(16) NOT NULL,
          admin_note VARCHAR(2000) NOT NULL,
          created_at DATETIME NOT NULL,
          last_login_at DATETIME
        );
        CREATE TABLE accounts (
          id INTEGER NOT NULL PRIMARY KEY,
          user_id INTEGER NOT NULL,
          platform VARCHAR(16) NOT NULL,
          identifier VARCHAR(64) NOT NULL,
          verified_at DATETIME,
          verify_token VARCHAR(32),
          verify_expires_at DATETIME,
          created_at DATETIME NOT NULL
        );
        INSERT INTO users VALUES
          (1,'24125023005','x','root',2024,1,'25','02','root','active','',
           datetime('now'),NULL);
        INSERT INTO accounts VALUES
          (1,1,'codeforces','tester',datetime('now'),NULL,NULL,datetime('now'));
        """
    )
    conn.commit()
    conn.close()
    return path


def _setup(tmp_path, monkeypatch, db_path):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{db_path}")

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()
    return db_module


def test_migration_adds_missing_columns(tmp_path, monkeypatch, old_db):
    db_module = _setup(tmp_path, monkeypatch, old_db)

    from app.migrate import run_migration

    added = run_migration()
    assert added > 0

    conn = sqlite3.connect(old_db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    for expected in [
        "rating",
        "max_rating",
        "display_name",
        "rating_fetched_at",
        "solved_fetched_at",
        "last_active_at",
        "last_error",
        "consecutive_failures",
        "rating_failures",
    ]:
        assert expected in columns, expected

    db_module.reset_engine()


def test_migration_is_idempotent(tmp_path, monkeypatch, old_db):
    db_module = _setup(tmp_path, monkeypatch, old_db)

    from app.migrate import run_migration

    assert run_migration() > 0
    # 第二次不该再改任何东西。
    assert run_migration() == 0

    db_module.reset_engine()


def test_migration_preserves_existing_rows(tmp_path, monkeypatch, old_db):
    """迁移不能丢数据。"""
    db_module = _setup(tmp_path, monkeypatch, old_db)

    from app.migrate import run_migration

    run_migration()

    conn = sqlite3.connect(old_db)
    users = list(conn.execute("SELECT student_id, display_name FROM users"))
    accounts = list(conn.execute("SELECT platform, identifier FROM accounts"))
    assert users == [("24125023005", "root")]
    assert accounts == [("codeforces", "tester")]

    db_module.reset_engine()


def test_new_integer_columns_default_to_zero_not_null(tmp_path, monkeypatch, old_db):
    """新增的整数列要有 DEFAULT 0，不能是 NULL。

    不给默认值时 SQLite 会把已有行填成 NULL，而模型里这些字段是「非空
    整数」，代码读出来是 None。多数情况下 None 恰好是 falsy 所以看不出
    问题，但 consecutive_failures 参与乘法时会直接报错。
    """
    db_module = _setup(tmp_path, monkeypatch, old_db)

    from app.migrate import run_migration

    run_migration()

    conn = sqlite3.connect(old_db)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(accounts)")]
    row = dict(zip(columns, list(conn.execute("SELECT * FROM accounts"))[0], strict=True))

    assert row["consecutive_failures"] == 0
    assert row["rating_failures"] == 0
    # 可空的业务字段仍应是 NULL，不该被硬塞一个假值。
    assert row["rating"] is None
    assert row["last_active_at"] is None

    db_module.reset_engine()


def test_check_mode_does_not_modify(tmp_path, monkeypatch, old_db):
    db_module = _setup(tmp_path, monkeypatch, old_db)

    from app.migrate import run_migration

    count = run_migration(check_only=True)
    assert count > 0

    conn = sqlite3.connect(old_db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    assert "rating" not in columns

    db_module.reset_engine()
