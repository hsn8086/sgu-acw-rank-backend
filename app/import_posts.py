"""把前端 content collection 里的 markdown 导入 posts 表。

一次性脚本：blog 从 git 里的文件搬到数据库后，老文章要跟过去。

用法::

    python -m app.import_posts /path/to/frontend/src/content/blog

幂等：slug 已存在就跳过，再跑一遍不会重复插。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from sqlmodel import Session, select

from .db import get_engine, init_db
from .models import Post, User


def _parse(path: Path) -> tuple[str, int, str]:
    """读出 title / sort_order / body。

    frontmatter 是 ``---\\ntitle: "x"\\nsortOrder: 100\\n---`` 这种。
    没用 yaml 库——frontmatter 只有两个键，正则够了。
    """
    text = path.read_text(encoding="utf-8")
    title = path.stem
    sort_order = 0
    body = text

    m = re.match(r"^---\n([\s\S]*?)\n---\n*", text)
    if m:
        fm = m.group(1)
        t = re.search(r'^title:\s*["\']?(.*?)["\']?\s*$', fm, re.M)
        if t:
            title = t.group(1)
        s = re.search(r"^sortOrder:\s*(\d+)", fm, re.M)
        if s:
            sort_order = int(s.group(1))
        body = text[m.end():]

    return title, sort_order, body.strip()


def run(directory: str, *, author_id: int | None = None) -> int:
    init_db()
    dirpath = Path(directory)
    if not dirpath.is_dir():
        print(f"目录不存在：{dirpath}", file=sys.stderr)
        return 1

    imported = 0
    with Session(get_engine()) as db:
        if author_id is None:
            root = db.exec(select(User).where(User.role == "root")).first()
            author_id = root.id if root else None
        if author_id is None:
            print("找不到 root 用户作为作者", file=sys.stderr)
            return 1

        for path in sorted(dirpath.glob("*.md")):
            slug = path.stem
            if db.exec(select(Post).where(Post.slug == slug)).first():
                print(f"跳过（已存在）：{slug}")
                continue
            title, sort_order, body = _parse(path)
            db.add(
                Post(
                    slug=slug,
                    title=title,
                    body=body,
                    sort_order=sort_order,
                    author_id=author_id,
                )
            )
            imported += 1
            print(f"导入：{slug} → 《{title}》（sort={sort_order}）")
        db.commit()

    print(f"共导入 {imported} 篇")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python -m app.import_posts <markdown 目录>", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(run(sys.argv[1]))
