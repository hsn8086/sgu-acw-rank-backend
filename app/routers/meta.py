"""Health and bootstrap-status endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from ..db import get_session
from ..deps import require_origin_secret
from ..models import User

#: Liveness probe. Deliberately unguarded so a local monitor can reach it
#: without the shared secret, and it touches nothing that can fail slowly.
health_router = APIRouter(tags=["meta"])

#: Everything else sits behind the origin guard, so a request that bypassed
#: Cloudflare cannot reach it even if it learns the origin address.
router = APIRouter(tags=["meta"], dependencies=[Depends(require_origin_secret)])


@health_router.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@router.get("/bootstrap")
def bootstrap_status(db: Session = Depends(get_session)) -> dict:
    """Whether the instance has any account yet.

    The frontend uses this to decide between showing a "first run" hint and the
    normal login form. It reveals only a count, never any account data.
    """
    total = db.exec(select(func.count()).select_from(User)).one()
    return {"has_users": bool(total), "user_count": int(total)}
