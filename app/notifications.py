"""管理员故障站内信。写入与业务事务一起提交，重复故障不反复变成未读。"""

from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from .models import AdminNotification, utcnow


def report(db: Session, fingerprint: str, title: str, body: str, href: str = "/admin") -> None:
    now = utcnow()
    # 并行请求首次报告同一故障时也只能生成一条。
    db.exec(
        insert(AdminNotification)
        .values(
            fingerprint=fingerprint,
            title=title,
            body=body,
            href=href,
            occurrences=0,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["fingerprint"])
    )
    row = db.exec(
        select(AdminNotification).where(
            AdminNotification.fingerprint == fingerprint,
        )
    ).one()
    if row.resolved_at is not None:
        row.revision += 1
    row.title = title
    row.body = body
    row.href = href
    row.occurrences += 1
    row.updated_at = now
    row.resolved_at = None
    db.add(row)


def resolve(db: Session, fingerprint: str) -> None:
    row = db.exec(
        select(AdminNotification).where(
            AdminNotification.fingerprint == fingerprint,
        )
    ).first()
    if row is None or row.resolved_at is not None:
        return
    row.resolved_at = utcnow()
    row.updated_at = row.resolved_at
    row.revision += 1
    db.add(row)
