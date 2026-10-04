"""给已有的库补上新增的列。

``SQLModel.metadata.create_all`` 只会建缺的表，**不会给已有的表加列**。
新版本给 accounts 加了 rating、display_name、各种时间戳，给 users 之类的
表可能也会陆续加字段，所以需要一个显式的迁移步骤。

做法是拿模型定义的列和库里的实际列对比，缺什么就 ``ALTER TABLE ADD COLUMN``。
SQLite 支持加列，而且加列是 O(1) 的元数据操作，不重写表。

用法：

    python -m app.migrate          # 补列
    python -m app.migrate --check  # 只看有没有缺的，不改

只处理加列。删列、改类型、加约束这些 SQLite 支持得不好，真需要时应该
另写迁移并配合数据搬运，不该由这个脚本悄悄做。

除了加列，这里还修**历史数据格式**。加列是结构问题，格式是内容问题，
但两者都是「升级一次就完事」的事，分两个脚本反而容易漏跑一个。
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import inspect
from sqlmodel import SQLModel

from .db import get_engine, init_db

logger = logging.getLogger("sgu_acw.migrate")


def _default_clause(column) -> str:
    """把模型上的默认值渲染成 ``DEFAULT ...`` 子句，没有则返回空串。

    只处理标量默认值（数字、字符串、布尔）。带 ``default_factory`` 的字段
    （比如各种 ``created_at``）不加默认值，因为它们的值依赖运行时，SQL 层
    没法表达；这些列本来就允许为空。
    """
    default = column.default
    if default is None or default.is_callable:
        return ""

    value = default.arg
    if isinstance(value, bool):
        return f" DEFAULT {1 if value else 0}"
    if isinstance(value, int | float):
        return f" DEFAULT {value}"
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f" DEFAULT '{escaped}'"
    return ""


def missing_columns() -> dict[str, list[tuple[str, str]]]:
    """返回 ``{表名: [(列名, 类型), ...]}``，只含库里缺的列。"""
    from . import models  # noqa: F401  导入即注册表定义

    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    result: dict[str, list[tuple[str, str]]] = {}
    for table_name, table in SQLModel.metadata.tables.items():
        if table_name not in existing_tables:
            # 新表交给 create_all 建，这里不管。
            continue

        have = {col["name"] for col in inspector.get_columns(table_name)}
        missing: list[tuple[str, str]] = []
        for column in table.columns:
            if column.name in have:
                continue
            # 用 SQLAlchemy 的方言编译器把类型渲染成 SQLite 能懂的写法。
            col_type = column.type.compile(dialect=engine.dialect)

            # 一定要带 DEFAULT。SQLite 的 ADD COLUMN 不给默认值时，已有的行
            # 会填 NULL；而模型里这些字段是「非空整数」，代码读出来是 None。
            # 多数情况下 None 恰好是 falsy 所以看不出问题，但那是运气——
            # 例如 consecutive_failures 参与乘法时会直接报错。
            default = _default_clause(column)
            missing.append((column.name, f"{col_type}{default}"))
        if missing:
            result[table_name] = missing
    return result


def _fix_nowcoder_keys(*, check_only: bool = False) -> int:
    """给牛客的历史题号补上来源前缀。

    早期的题单功能让管理员直接填数字（``233234``），但过题记录里存的是带
    来源前缀的 key（``nowcoder:contest:233234``）。两边对不上，于是：

    * 完成度永远算成 0 —— 比对时一个是裸数字一个有前缀，永远不相等；
    * 题名抓不到 —— 抓取按 ``来源:题号`` 解析，裸数字走不进任何分支。

    数字在牛客上是竞赛站的题目 id（主站的题是 uuid，不是数字），所以统一
    补成 ``nowcoder:contest:{数字}``。

    两张表都要修：``problem_list_items``（题单里有什么）和
    ``problem_cache``（抓回来的题目元信息）。只修一张的话，另一张仍然
    对不上。
    """
    from sqlalchemy import text

    fixed = 0
    tables = ("problem_list_items", "problem_cache")
    with get_engine().begin() as connection:
        for table in tables:
            rows = connection.execute(
                text(
                    f"SELECT id, key FROM {table} "  # noqa: S608  表名是常量
                    "WHERE platform = 'nowcoder' "
                    "AND key GLOB '[0-9]*' "
                    "AND key NOT LIKE '%:%'"
                )
            ).fetchall()
            if not rows:
                continue
            if check_only:
                logger.warning("%s 有 %d 条牛客题号缺前缀", table, len(rows))
                fixed += len(rows)
                continue

            for row_id, key in rows:
                connection.execute(
                    text(f"UPDATE {table} SET key = :key WHERE id = :id"),  # noqa: S608
                    {"key": f"nowcoder:contest:{key}", "id": row_id},
                )
            logger.info("%s 修了 %d 条牛客题号", table, len(rows))
            fixed += len(rows)

    return fixed


def _rename_columns(*, check_only: bool = False) -> int:
    """把改过名字的列迁过去。

    ``missing_columns`` 只处理加列，不会发现「模型里叫 A、库里叫 B」的情况——
    那种情况下它会以为 A 不存在，试图 ALTER TABLE ADD COLUMN。如果 B 上
    有 NOT NULL 约束就会失败，没有的话更糟：加出一个空列，旧数据全丢。

    SQLite 3.25+ 支持 RENAME COLUMN，是元数据操作，不重写表。生产用的是
    3.45，可以放心用。

    **只在旧名存在、新名不存在时改**。两个都在说明改过了，什么都不做；
    只有新名则说明是新建的库，也不用管。
    """
    from sqlalchemy import inspect, text

    # (表, 旧列名, 新列名)
    RENAMES: list[tuple[str, str, str]] = [
        # 题解里的 C++ 代码从 17 提到 20（ranges、结构化绑定），
        # 字段名一直没跟着改，容易误导。
        ("editorials", "code_cpp17", "code_cpp20"),
    ]

    inspector = inspect(get_engine())
    changed = 0
    with get_engine().begin() as connection:
        for table, old, new in RENAMES:
            if not inspector.has_table(table):
                continue
            columns = {col["name"] for col in inspector.get_columns(table)}
            if old not in columns or new in columns:
                continue
            if check_only:
                logger.warning("%s.%s 需要改名为 %s", table, old, new)
                changed += 1
                continue
            connection.execute(
                text(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')
            )
            logger.info("改名：%s.%s -> %s", table, old, new)
            changed += 1
    return changed


def run_migration(*, check_only: bool = False) -> int:
    """补列。返回补了几列。"""
    init_db()  # 先把缺的表建出来
    # 改名要在补列之前跑：改完之后模型和库对得上，missing_columns 才不会
    # 把「名字不同」误判成「缺列」。
    renamed = _rename_columns(check_only=check_only)

    fixed = _fix_nowcoder_keys(check_only=check_only)

    pending = missing_columns()
    if not pending:
        logger.info("没有缺的列")
        return renamed + fixed

    total = 0
    for table_name, columns in pending.items():
        for name, col_type in columns:
            if check_only:
                logger.warning("缺列 %s.%s (%s)", table_name, name, col_type)
                total += 1
                continue

            # 用原生 SQL 加列。SQLite 的 ALTER TABLE ADD COLUMN 不支持加
            # 非空且无默认值的列，所以新列都必须是可空的或者有默认值——
            # 这一点由模型定义保证（新增字段都给了默认值）。
            statement = f'ALTER TABLE "{table_name}" ADD COLUMN "{name}" {col_type}'
            logger.info("加列：%s", statement)
            with get_engine().begin() as connection:
                connection.exec_driver_sql(statement)
            total += 1

    return renamed + total + fixed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    check_only = "--check" in sys.argv
    count = run_migration(check_only=check_only)
    if check_only:
        print(f"有 {count} 处待处理" if count else "结构和数据都是最新的")
    else:
        print(f"处理了 {count} 处" if count else "结构和数据已经是最新的")


if __name__ == "__main__":
    main()
