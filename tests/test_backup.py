"""备份的测试。

重点是「备份必须完整」。这个模块存在的理由就是 ``cp`` 不可靠：库跑在 WAL
模式下，主 ``.db`` 文件可能长时间不更新，只复制它会得到过时甚至空的快照。
线上踩过一次，一份备份打开后是 ``no such table: users``。
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def wal_db(tmp_path, monkeypatch):
    """造一个数据都在 WAL 里、尚未合并回主文件的库。

    这是最容易被 ``cp`` 骗到的状态：主文件很小，数据全在 ``-wal``。
    """
    path = tmp_path / "api.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    # 关掉自动合并，保证数据留在 WAL 里。
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, ident TEXT);
        INSERT INTO users (name) VALUES ('甲'), ('乙');
        INSERT INTO accounts (ident) VALUES ('a'), ('b'), ('c');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{path}")

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()
    yield path
    db_module.reset_engine()
    get_settings.cache_clear()


def test_backup_captures_data_in_wal(wal_db):
    """备份要拿到还在 WAL 里的数据。

    这正是 ``cp`` 做不到的：只复制主文件会漏掉未合并的写入。
    """
    from app.backup import run_backup

    target = run_backup(keep=7)
    assert target is not None and target.exists()

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    assert list(conn.execute("SELECT COUNT(*) FROM users"))[0][0] == 2
    assert list(conn.execute("SELECT COUNT(*) FROM accounts"))[0][0] == 3
    assert {row[0] for row in conn.execute("SELECT name FROM users")} == {"甲", "乙"}
    conn.close()


def test_backup_is_self_contained(wal_db):
    """备份是一个独立的完整库，不依赖原来的 WAL。"""
    from app.backup import run_backup

    target = run_backup(keep=7)

    # 把原库连同 WAL 一起挪走，备份仍应能读。
    wal = wal_db.with_name(wal_db.name + "-wal")
    shm = wal_db.with_name(wal_db.name + "-shm")
    for path in (wal, shm):
        if path.exists():
            path.unlink()

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    assert list(conn.execute("SELECT COUNT(*) FROM users"))[0][0] == 2
    conn.close()


def test_backup_prunes_old_files(wal_db):
    """只保留指定份数。"""
    import time

    from app.backup import list_backups, run_backup

    for _ in range(4):
        run_backup(keep=2)
        # 文件名精确到秒，隔一下才有不同的名字。
        time.sleep(1.05)

    names = [name for name, _ in list_backups()]
    assert len(names) == 2


def test_backup_skips_duplicate_within_same_second(wal_db):
    """同一秒内重复调用不该覆盖已有备份。"""
    from app.backup import run_backup

    first = run_backup(keep=7)
    second = run_backup(keep=7)
    assert first == second


def test_backup_rejects_non_sqlite(monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", "postgresql://localhost/x")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.backup import run_backup

    with pytest.raises(RuntimeError, match="只支持 SQLite"):
        run_backup()

    get_settings.cache_clear()


def test_list_backups_fails_loudly_when_database_missing(tmp_path, monkeypatch):
    """库路径不对时要报错，而不是返回空列表。

    手动跑时很容易忘了带 SGU_ACW_DATABASE_URL（那个环境文件只有 systemd
    会加载），路径就默认到工作目录下了。空列表会被误读成「没有备份」，
    而实际是「看错地方了」。
    """
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'nope.db'}")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.backup import list_backups

    with pytest.raises(FileNotFoundError, match="数据库不存在"):
        list_backups()

    get_settings.cache_clear()
