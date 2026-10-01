"""Table definitions.

Timestamps are timezone-aware UTC throughout. SQLModel's default datetime
column is a ``UTCDateTime`` that rejects naive values and normalises everything
to UTC on the way in and out, so ``utcnow()`` below returns an aware value and
comparisons between stored and computed timestamps stay consistent.

Note: this module deliberately does *not* use ``from __future__ import
annotations``. Postponed evaluation turns the ``Relationship`` annotations into
plain strings, which SQLModel cannot resolve back to the related class.
"""

from datetime import UTC, datetime, timedelta

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class Role:
    """Account roles, ordered from most to least privileged."""

    ROOT = "root"
    ADMIN = "admin"
    MEMBER = "member"

    ALL = (ROOT, ADMIN, MEMBER)
    #: Roles that may reach the admin endpoints.
    ADMINISTRATIVE = (ROOT, ADMIN)


class UserStatus:
    ACTIVE = "active"
    DISABLED = "disabled"


class Platform:
    """Supported competitive-programming platforms."""

    CODEFORCES = "codeforces"
    ATCODER = "atcoder"
    LUOGU = "luogu"
    NOWCODER = "nowcoder"

    ALL = (CODEFORCES, ATCODER, LUOGU, NOWCODER)
    #: How the identifier is written down, shown in error messages.
    LABELS = {
        CODEFORCES: "Codeforces",
        ATCODER: "AtCoder",
        LUOGU: "洛谷",
        NOWCODER: "牛客",
    }


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)

    #: 学号，同时是登录名。解析出的字段冗余存放，榜单查询不再重新解析。
    student_id: str = Field(index=True, unique=True, max_length=16)
    password_hash: str
    display_name: str = Field(max_length=64)

    enroll_year: int
    level: int
    college_code: str = Field(max_length=4)
    major_code: str = Field(max_length=4)

    role: str = Field(default=Role.MEMBER, max_length=16, index=True)
    status: str = Field(default=UserStatus.ACTIVE, max_length=16, index=True)

    #: 仅管理员可见、可改。用来记录核实过程，例如某个学号曾被他人占用。
    admin_note: str = Field(default="", max_length=2000)

    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: datetime | None = None

    accounts: list["Account"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    @property
    def is_admin(self) -> bool:
        return self.role in Role.ADMINISTRATIVE

    @property
    def is_root(self) -> bool:
        return self.role == Role.ROOT

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE


class Invite(SQLModel, table=True):
    """An invite code.

    Two limits apply at once and whichever trips first wins: ``max_uses`` caps
    how many accounts the code can create, ``expires_at`` caps how long it
    stays valid. A code is spent when ``use_count`` reaches ``max_uses``.
    """

    __tablename__ = "invites"

    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True, max_length=32)

    #: Role granted to whoever registers with this code. Admin codes are how a
    #: root hands out administrative access.
    grants_role: str = Field(default=Role.MEMBER, max_length=16)

    max_uses: int = Field(default=1)
    use_count: int = Field(default=0)
    expires_at: datetime

    revoked_at: datetime | None = None
    #: 管理员备注，仅管理员可见。
    note: str = Field(default="", max_length=500)

    created_by: int | None = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_expired(self) -> bool:
        return utcnow() >= self.expires_at

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_exhausted(self) -> bool:
        return self.use_count >= self.max_uses

    @property
    def is_usable(self) -> bool:
        return not (self.is_expired or self.is_revoked or self.is_exhausted)

    def expires_in(self, hours: int) -> None:
        self.expires_at = utcnow() + timedelta(hours=hours)


class UserSession(SQLModel, table=True):
    """A logged-in browser.

    Only the SHA-256 of the token is stored, so a database leak does not hand
    out working sessions.

    Named ``UserSession`` rather than ``Session`` so it does not shadow
    ``sqlmodel.Session`` at every import site.
    """

    __tablename__ = "sessions"

    id: int | None = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True, max_length=64)
    user_id: int = Field(foreign_key="users.id", index=True)

    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    last_seen_at: datetime = Field(default_factory=utcnow)

    ip: str = Field(default="", max_length=64)
    user_agent: str = Field(default="", max_length=256)

    @property
    def is_expired(self) -> bool:
        return utcnow() >= self.expires_at


class Account(SQLModel, table=True):
    """A platform account owned by a member.

    One member may hold several accounts on the same platform (a main and an
    alt), so the uniqueness constraint is on the platform/identifier pair rather
    than on the member.
    """

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("platform", "identifier", name="uq_account_platform_identifier"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    platform: str = Field(max_length=16, index=True)
    #: Codeforces/AtCoder handle, or the numeric uid on 洛谷/牛客.
    identifier: str = Field(max_length=64)

    verified_at: datetime | None = None
    #: The ``SGU-xxxx`` string the member pastes into their platform profile.
    verify_token: str | None = Field(default=None, max_length=32)
    verify_expires_at: datetime | None = None

    created_at: datetime = Field(default_factory=utcnow)

    user: User | None = Relationship(back_populates="accounts")

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None


class SolvedSnapshot(SQLModel, table=True):
    """某账号最近一次抓到的已过题目集合。

    只对没有时间戳的平台有意义：洛谷只给出累计通过的题目集合，拿不到
    「什么时候过的」。于是每次同步时与上一份快照比对，多出来的题就算作
    这次同步之前那段时间的新增。CF 和 AtCoder 有时间戳，不需要这张表，
    但也会存一份，便于排查抓取是否正常。
    """

    __tablename__ = "solved_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accounts.id", unique=True, index=True)

    #: 题目标识的 JSON 数组。
    problems_json: str = Field(default="[]")
    problem_count: int = Field(default=0)

    fetched_at: datetime = Field(default_factory=utcnow)
    #: 上一次抓取的时间，用于判断两次抓取之间隔了多久。
    previous_fetched_at: datetime | None = None


class DailyProgress(SQLModel, table=True):
    """每个平台账号每天的过题数。榜单直接读这张表，不再实时抓平台。

    键是「账号 + 日期」而不是「人 + 平台 + 日期」：一个成员在同一平台
    可以绑多个账号（主号加小号），按人作键的话两个账号会互相覆盖，
    只留下最后同步的那个。展示时再按人汇总。

    日期按东八区切分：成员说的「今天」是北京时间，而 CF 和 AtCoder 的
    时间戳是 UTC，直接按 UTC 分日会让晚上的提交算到第二天。
    """

    __tablename__ = "daily_progress"
    __table_args__ = (UniqueConstraint("account_id", "date", name="uq_progress_account_date"),)

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accounts.id", index=True)

    platform: str = Field(max_length=16, index=True)
    #: YYYY-MM-DD，东八区。
    date: str = Field(max_length=10, index=True)
    solved_count: int = Field(default=0)

    updated_at: datetime = Field(default_factory=utcnow)


class SyncRun(SQLModel, table=True):
    """一次同步的记录，用来在管理页展示「上次同步是什么时候、有没有出错」。"""

    __tablename__ = "sync_runs"

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    account_count: int = Field(default=0)
    ok_count: int = Field(default=0)
    error_count: int = Field(default=0)
    #: 出错账号的说明，JSON 数组。
    errors_json: str = Field(default="[]")

    @property
    def is_running(self) -> bool:
        return self.finished_at is None
