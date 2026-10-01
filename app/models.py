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
from typing import ClassVar

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


#: 秒数常量。调度相关的判断都按天算，写个名字比一串 86400 清楚。
DAY_SECONDS = 86400


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


class InviteUse(SQLModel, table=True):
    """谁用邀请码注册的。

    ``Invite.use_count`` 只知道用了几次，不知道被谁用了。出事时（比如码被
    转发出去）光看次数查不出人来，所以每次兑换单独记一行。
    """

    __tablename__ = "invite_uses"

    id: int | None = Field(default=None, primary_key=True)
    invite_id: int = Field(foreign_key="invites.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    used_at: datetime = Field(default_factory=utcnow)


class RefreshQuota(SQLModel, table=True):
    """手动刷新练习数据的额度。

    抓取本身是定时任务在做，成员不需要操心。但有人刚打完一场想立刻看到
    数字变化，所以给一个手动刷新的口子，每周三次，防止有人反复点。

    按周记账：``week_start`` 是那一周的周一（东八区），换周自然重置，
    不需要额外的清理任务。
    """

    __tablename__ = "refresh_quotas"
    __table_args__ = (UniqueConstraint("user_id", "week_start", name="uq_quota_user_week"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    #: 那一周的周一，YYYY-MM-DD（东八区）。
    week_start: str = Field(max_length=10, index=True)
    used: int = Field(default=0)

    #: 每周可用的次数。标成 ClassVar，否则 Pydantic 会把它当成表字段。
    WEEKLY_LIMIT: ClassVar[int] = 3

    @property
    def remaining(self) -> int:
        return max(0, self.WEEKLY_LIMIT - self.used)


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

    # ------------------------------------------------------------------ #
    # 缓存的平台数据
    # ------------------------------------------------------------------ #
    #
    # rating 变化很慢（只有打完比赛才变），所以每天查一次就够；过题数则要
    # 勤一些，否则榜单看起来像是不动的。两类数据分开记时间戳，各自的调度
    # 周期才能不同。

    rating: int | None = None
    max_rating: int | None = None
    #: 平台上的昵称，与 identifier 不一定相同（牛客的 identifier 是数字 uid）。
    display_name: str | None = Field(default=None, max_length=64)
    rating_fetched_at: datetime | None = None

    solved_fetched_at: datetime | None = None
    #: 最近一次**拓到新题**是什么时候。与 solved_fetched_at 不同：后者只是
    #: 「最后一次去看」，这个才是「最后一次看到变化」。调度靠它，否则一个
    #: 从不做题的人也会因为反复去看而一直占用高频额度。
    last_active_at: datetime | None = None
    #: 最近一次抓取出错的原因。有值时调度器会把它排得更靠前重试。
    last_error: str | None = Field(default=None, max_length=500)

    created_at: datetime = Field(default_factory=utcnow)

    user: User | None = Relationship(back_populates="accounts")

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    def sync_interval_seconds(self, *, now: datetime, active: int, idle: int, fresh: int) -> int:
        """这个账号现在该隔多久同步一次。

        三个档位，从勤到懒：

        * ``fresh``：注册不久，还看不出习惯，勤看着点。
        * ``active``：最近三天内拓到过新题，属于在用的人。
        * ``idle``：一直没动静，降低频率省请求。

        依据是「最后一次拓到新题」而不是「最后一次同步」，所以一个从不
        做题的人会自然落到最低频，不会因为反复查看而占着高频额度。
        """
        if self.last_active_at is not None:
            idle_for = (now - self.last_active_at).total_seconds()
            return active if idle_for < 3 * DAY_SECONDS else idle

        # 还没拓到过任何新题：看注册多久了。
        age = (now - self.created_at).total_seconds()
        return fresh if age < 7 * DAY_SECONDS else idle

    def is_due(self, *, now: datetime, active: int, idle: int, fresh: int) -> bool:
        """现在是否到了该同步的时候。"""
        if self.solved_fetched_at is None:
            return True
        interval = self.sync_interval_seconds(now=now, active=active, idle=idle, fresh=fresh)
        return (now - self.solved_fetched_at).total_seconds() >= interval


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
