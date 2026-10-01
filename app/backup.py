"""数据库备份。

**不要用 ``cp`` 备份 SQLite。** 这个库跑在 WAL 模式下，新写入先进 ``-wal``
文件，主 ``.db`` 文件可能长时间不更新。只复制主文件会得到一个过时甚至空的
快照——线上真的这么踩过：一份备份打开后是 ``no such table: users``。

正确做法是让 SQLite 自己导出一份一致快照：

* ``VACUUM INTO``：把当前所有内容（含 WAL 里未合并的部分）写成一个新的
  完整文件。这是我们要的。
* 或者停服务后把 ``.db`` / ``-wal`` / ``-shm`` 三个文件一起复制。

用法：

    python -m app.backup              # 备份，保留最近 7 份
    python -m app.backup --keep 14    # 保留 14 份
    python -m app.backup --list       # 列出已有的备份
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, text

from .config import get_settings
from .db import get_engine

logger = logging.getLogger("sgu_acw.backup")

#: 备份文件名的格式。用时间戳排序，不用看文件的 mtime。
STAMP = "%Y%m%d-%H%M%S"
BACKUP_RE = re.compile(r"^api\.db\.bak-(\d{8}-\d{6})$")


def _db_path() -> Path:
    """从配置里取出实际的文件路径。只支持 SQLite。"""
    url = get_settings().database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise RuntimeError(f"只支持 SQLite，当前是 {url}")
    # sqlite:///path 与 sqlite:////abs/path 都是「去掉前缀后就是路径」。
    return Path(url[len(prefix) :]).resolve()


def run_backup(*, keep: int = 7) -> Path | None:
    """导出一份一致快照，并清理过旧的备份。返回新备份的路径。"""
    db_path = _db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")

    stamp = datetime.now(UTC).strftime(STAMP)
    target = db_path.with_name(f"{db_path.name}.bak-{stamp}")

    if target.exists():
        logger.info("这一秒已经备份过了，跳过")
        return target

    # VACUUM INTO 会把当前全部内容写成新文件，包括还在 WAL 里的部分。
    # 目标文件必须不存在，所以上面先查了一次。
    with Session(get_engine()) as db:
        # 参数化在这里不管用：VACUUM 的路径不能是绑定参数。
        # 路径来自配置而不是请求，且做了转义。
        db.exec(text(f"VACUUM INTO '{target.as_posix().replace(chr(39), chr(39) * 2)}'"))  # type: ignore[call-overload]

    logger.info("备份完成：%s", target)
    _prune(db_path, keep=keep)
    return target


def _prune(db_path: Path, *, keep: int) -> None:
    """只保留最近 ``keep`` 份备份。"""
    backups = sorted(
        (
            (m.group(1), p)
            for p in db_path.parent.glob(f"{db_path.name}.bak-*")
            if (m := BACKUP_RE.match(p.name))
        ),
        reverse=True,
    )
    for _, path in backups[keep:]:
        logger.info("删除旧备份：%s", path.name)
        path.unlink()


def list_backups() -> list[tuple[str, int]]:
    """列出备份：``(文件名, 字节数)``，从新到旧。"""
    db_path = _db_path()

    # 库不存在时直接报错，不要静默返回空列表。
    #
    # 手动跑时很容易忘了带上 SGU_ACW_DATABASE_URL（那个环境文件只有 systemd
    # 会加载），于是路径默认为工作目录下的 sgu-acw.db，globbing 什么都找不到。
    # 空列表会被误读成「没有备份」，而实际是「看错地方了」。
    if not db_path.exists():
        raise FileNotFoundError(
            f"数据库不存在：{db_path}\n手动执行时要带上 SGU_ACW_DATABASE_URL，或用 systemctl 跑。"
        )

    out: list[tuple[str, int]] = []
    for path in sorted(
        db_path.parent.glob(f"{db_path.name}.bak-*"),
        key=lambda p: p.name,
        reverse=True,
    ):
        out.append((path.name, path.stat().st_size))
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if "--list" in sys.argv:
        for name, size in list_backups():
            print(f"  {name:<40} {size / 1024:.0f} KB")
        return

    keep = 7
    if "--keep" in sys.argv:
        index = sys.argv.index("--keep")
        keep = int(sys.argv[index + 1])

    path = run_backup(keep=keep)
    if path is not None:
        print(f"备份完成：{path.name}（{path.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
