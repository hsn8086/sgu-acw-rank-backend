"""Application assembly.

The service is reached only through the Pages Function, which forwards
``sgu-acw.zh314.xyz/api/*`` here. That keeps the browser on one origin, so
cookies need no ``SameSite=None`` and there is no CORS configuration at all.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlmodel import Session, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import models
from .config import get_settings
from .db import get_engine, init_db
from .deps import require_origin_secret
from .models import Role, User, utcnow
from .routers import meta
from .security import hash_password
from .student_id import StudentIdError, parse_student_id

logger = logging.getLogger("sgu_acw")

API_PREFIX = "/api"


def bootstrap_root() -> None:
    """Create the seed root account on first run.

    Idempotent: once the student id exists this does nothing, so a restart never
    resets the password of an account that has been in use.
    """
    settings = get_settings()
    if not (settings.bootstrap_root_student_id and settings.bootstrap_root_password):
        return

    try:
        parsed = parse_student_id(settings.bootstrap_root_student_id)
    except StudentIdError:
        logger.error("bootstrap root student id is not a valid SGU student id; skipping")
        return

    with Session(get_engine()) as db:
        existing = db.exec(select(User).where(User.student_id == parsed.raw)).first()
        if existing is not None:
            return

        db.add(
            User(
                student_id=parsed.raw,
                password_hash=hash_password(settings.bootstrap_root_password),
                display_name="root",
                enroll_year=parsed.enroll_year,
                level=parsed.level,
                college_code=parsed.college_code,
                major_code=parsed.major_code,
                role=Role.ROOT,
                admin_note="首次启动自动创建",
                created_at=utcnow(),
            )
        )
        db.commit()
        logger.info("created bootstrap root account %s", parsed.raw)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bootstrap_root()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="SGU ACW Rank API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        redoc_url=None,
    )

    app.include_router(meta.health_router, prefix=API_PREFIX)
    app.include_router(meta.router, prefix=API_PREFIX)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get(f"{API_PREFIX}/config", dependencies=[Depends(require_origin_secret)])
    def public_config() -> dict:
        """Small, non-sensitive constants the frontend needs."""
        return {
            "platforms": [
                {"value": p, "label": models.Platform.LABELS[p]} for p in models.Platform.ALL
            ],
            "cookie_name": settings.cookie_name,
        }

    return app


app = create_app()
