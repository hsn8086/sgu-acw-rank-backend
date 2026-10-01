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
from .routers import accounts, admin, auth, meta, practice, users
from .security import hash_password
from .student_id import ROOT_STUDENT_ID, StudentIdError, is_root_student_id, parse_student_id

logger = logging.getLogger("sgu_acw")

API_PREFIX = "/api"


def bootstrap_root() -> None:
    """Create the seed root account on first run.

    Idempotent: once the student id exists this does nothing, so a restart never
    resets the password of an account that has been in use.

    root 的学号是十一个 0，不是真实学号——它不占用任何人的号，也不会跟人
    撞号。因此这里不跑学号解析，年级学院都填占位值。
    """
    settings = get_settings()
    if not settings.bootstrap_root_password:
        return

    student_id = (settings.bootstrap_root_student_id or ROOT_STUDENT_ID).strip()

    # 配了真实学号时仍然解析，方便从旧配置平滑过渡；但推荐用占位号。
    if is_root_student_id(student_id):
        parsed = None
    else:
        try:
            parsed = parse_student_id(student_id)
        except StudentIdError:
            logger.error(
                "bootstrap root student id %r is not valid; falling back to %s",
                student_id,
                ROOT_STUDENT_ID,
            )
            student_id = ROOT_STUDENT_ID
            parsed = None

    with Session(get_engine()) as db:
        existing = db.exec(select(User).where(User.student_id == student_id)).first()
        if existing is not None:
            return

        db.add(
            User(
                student_id=student_id,
                password_hash=hash_password(settings.bootstrap_root_password),
                display_name="root",
                enroll_year=parsed.enroll_year if parsed else 0,
                level=parsed.level if parsed else 0,
                college_code=parsed.college_code if parsed else "00",
                major_code=parsed.major_code if parsed else "00",
                role=Role.ROOT,
                admin_note="首次启动自动创建",
                created_at=utcnow(),
            )
        )
        db.commit()
        logger.info("created bootstrap root account %s", student_id)


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
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(users.router, prefix=API_PREFIX)
    app.include_router(accounts.router, prefix=API_PREFIX)
    app.include_router(practice.router, prefix=API_PREFIX)
    app.include_router(admin.router, prefix=API_PREFIX)

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
