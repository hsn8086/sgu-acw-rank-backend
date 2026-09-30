"""Application settings.

Every knob is an environment variable prefixed with ``SGU_ACW_``; a ``.env``
file next to the working directory is read as well, which is what the local
dev flow uses.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SGU_ACW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///./sgu-acw.db"

    # Sessions
    session_ttl_days: int = 30
    cookie_name: str = "sgu_acw_session"
    # The browser only ever talks to the Pages domain, so the cookie belongs to
    # that host and needs no Domain attribute. Secure stays on in production;
    # local http debugging flips it off.
    cookie_secure: bool = True
    cookie_domain: str | None = None

    # Shared secret with the Pages Function. Requests without it are rejected,
    # which stops anyone from bypassing Cloudflare and hitting the origin
    # directly. Empty disables the check (local development).
    origin_secret: str = ""

    # First-run bootstrap. When both are set and no user carries this student id
    # yet, a root account is created. Ignored on every later start.
    bootstrap_root_student_id: str = ""
    bootstrap_root_password: str = ""

    # Scrypt work factor. n must be a power of two; 2**14 keeps a login around
    # 50ms on the target VPS while staying far above the memory a GPU rig would
    # find worthwhile.
    scrypt_n: int = 2**14
    scrypt_r: int = 8
    scrypt_p: int = 1

    # Invite defaults offered in the admin UI.
    invite_default_max_uses: int = 1
    invite_default_ttl_hours: int = 24


@lru_cache
def get_settings() -> Settings:
    return Settings()
