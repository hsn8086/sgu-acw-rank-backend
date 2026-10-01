"""Application settings.

Every knob is an environment variable prefixed with ``SGU_ACW_``; a ``.env``
file next to the working directory is read as well, which is what the local
dev flow uses.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from .student_id import ROOT_STUDENT_ID


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

    # First-run bootstrap. When the password is set and no user carries this
    # student id yet, a root account is created. Ignored on every later start.
    #
    # 学号默认是十一个 0——那不是合法学号，所以没人能注册到它，也不会占掉
    # 真实号段。留空就用这个默认值。
    bootstrap_root_student_id: str = ROOT_STUDENT_ID
    bootstrap_root_password: str = ""

    # Scrypt work factor. n must be a power of two; 2**14 keeps a login around
    # 50ms on the target VPS while staying far above the memory a GPU rig would
    # find worthwhile. Tests lower this via SGU_ACW_SCRYPT_N to keep the suite
    # fast; production never overrides it.
    scrypt_n: int = 2**14
    scrypt_r: int = 8
    scrypt_p: int = 1

    # Invite defaults offered in the admin UI.
    invite_default_max_uses: int = 1
    invite_default_ttl_hours: int = 24

    # 同步周期（分钟）。过题数变化快，所以分档：刚注册的人还在养成习惯，
    # 看得勤一些；最近三天内拓到过新题的人属于在用；其余降低频率省请求。
    #
    # 之所以按「最后一次拓到新题」而不是「最后一次同步」分档，是为了让
    # 不做题的人自然落到最低档，而不是反复占用高频额度。
    sync_interval_fresh_minutes: int = 30
    sync_interval_active_minutes: int = 60
    sync_interval_idle_minutes: int = 360

    # rating 只在打完比赛后变，一天一次足够。
    rating_interval_minutes: int = 24 * 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
