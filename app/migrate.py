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


def run_migration(*, check_only: bool = False) -> int:
    """补列。返回补了几列。"""
    init_db()  # 先把缺的表建出来

    pending = missing_columns()
    if not pending:
        logger.info("没有缺的列")
        return 0

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

    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    check_only = "--check" in sys.argv
    count = run_migration(check_only=check_only)
    if check_only:
        print(f"缺 {count} 列" if count else "结构是最新的")
    else:
        print(f"补了 {count} 列" if count else "结构已经是最新的")


if __name__ == "__main__":
    main()
