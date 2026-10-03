"""把「哪些题有题面」回填到 problem_cache.has_statement。

题面存在生成器那边的离线索引里（``tools/generate-editorial/data/``），
来源是 CPRet 数据集。每日一题抽签要跳过没有题面的题，所以后端得知道
这份名单。

**为什么用回填而不是让后端去读索引文件**

后端和生成器是两套东西：后端跑在 web 进程里，生成器是个能跑几分钟的
独立进程。让 web 进程去读生成器的数据文件，会把两个部署单元绑在一起
（文件路径、版本、更新时机都得对齐）。而「这道题有没有题面」是个稳定
的事实，存进数据库之后两边就解耦了。

用法::

    python -m app.import_statements                 # 用默认路径
    python -m app.import_statements --path <file>   # 指定索引
    python -m app.import_statements --check         # 只看会改多少，不写库

索引文件是 JSON 数组，元素是题号字符串（如 ``1918A``）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlmodel import Session, select

from .db import get_engine, init_db
from .models import ProblemCache

#: 默认索引位置。相对仓库根目录。
DEFAULT_PATH = Path(__file__).resolve().parent.parent / (
    "tools/generate-editorial/data/have_statements.json"
)


def load_keys(path: Path) -> set[str]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"{path} 应该是 JSON 数组")
    return {str(key) for key in data}


def run(*, path: Path, check_only: bool = False) -> dict[str, int]:
    """回填。返回各类计数，方便在 CI 或运维时看一眼。"""
    if not path.exists():
        raise FileNotFoundError(
            f"找不到题面索引：{path}\n"
            "这个文件由 tools/generate-editorial 从 CPRet 数据集生成，"
            "不在版本库里。先跑一遍生成脚本，或用 --path 指定。"
        )

    keys = load_keys(path)

    with Session(get_engine()) as session:
        rows = session.exec(select(ProblemCache).where(ProblemCache.platform == "codeforces")).all()  # type: ignore[arg-type]

        marked = 0
        cleared = 0
        unchanged = 0

        for row in rows:
            want = row.key in keys
            if row.has_statement == want:
                unchanged += 1
                continue
            if check_only:
                if want:
                    marked += 1
                else:
                    cleared += 1
                continue
            row.has_statement = want
            session.add(row)
            if want:
                marked += 1
            else:
                cleared += 1

        if not check_only and (marked or cleared):
            session.commit()

    return {
        "index_size": len(keys),
        "total": len(rows),
        "marked": marked,
        "cleared": cleared,
        "unchanged": unchanged,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH, help="题面索引文件")
    parser.add_argument("--check", action="store_true", help="只统计，不写库")
    args = parser.parse_args()

    init_db()
    result = run(path=args.path, check_only=args.check)

    verb = "将标记" if args.check else "已标记"
    print(f"索引里有 {result['index_size']} 道题")
    print(f"缓存里 CF 题共 {result['total']} 道")
    print(f"{verb} {result['marked']} 道为「有题面」，清掉 {result['cleared']} 道")
    print(f"无需改动 {result['unchanged']} 道")


if __name__ == "__main__":
    main()
