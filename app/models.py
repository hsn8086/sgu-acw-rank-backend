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

from . import academic


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


class ProblemListKind:
    """题单的两种形态。"""

    #: 题单的集合，里面放别的题单。
    COLLECTION = "collection"
    #: 题目清单。
    LIST = "list"

    ALL = (COLLECTION, LIST)


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
    #: 班级序号，学号第 8 位。与学院专业一起才是完整班级，如「软件工程 3 班」。
    class_no: int = Field(default=0)

    #: 学院名与专业名的**人工覆盖值**。为空时按学号里的码查表。
    #:
    #: 需要可覆盖是因为联培码（94-97）是共用的，同一个码下挂着不同学院的
    #: 专业，光看码推不出准确的学院名。
    #:
    #: 列名带 override 后缀是有意的：接口里的 college_name 是解析结果
    #: （见下面的属性），两者不是一回事，名字上分开免得混。
    college_override: str | None = Field(default=None, max_length=64)
    major_override: str | None = Field(default=None, max_length=64)

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
    def college_name(self) -> str | None:
        """学院名。人工改过就用改的，否则按学号里的码查表。"""
        return self.college_override or academic.college_name(self.college_code)

    @property
    def major_name(self) -> str | None:
        """专业名。同上。"""
        return self.major_override or academic.major_name(self.college_code, self.major_code)

    @property
    def class_label(self) -> str | None:
        """班级简称，如「信工软工 3 班」。

        单独一个「3 班」没有意义——得知道是哪个专业的 3 班。

        用简称而不是全名：全名拼出来是「信息工程学院 · 软件工程 ·
        软件工程 3 班」，一行放不下，窄屏更是一团。「信工软工 3 班」
        一眼能读完。
        """
        if not self.class_no:
            return None

        from .academic import short_college, short_major

        prefix = f"{short_college(self.college_name)}{short_major(self.major_name)}"
        return f"{prefix} {self.class_no} 班" if prefix else f"{self.class_no} 班"

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
    数字变化，所以给一个手动刷新的口子，每周七次，防止有人反复点。

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
    WEEKLY_LIMIT: ClassVar[int] = 7

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
    #: 上次尝试验证的时间。用于防抖：每次验证都要去平台读一次资料，连点不仅
    #: 浪费请求，还可能把平台的 WAF 惹毛——那会连带影响所有人的抓取。
    #:
    #: 无论成功还是失败都记，否则失败时连点照样刷。
    verify_attempted_at: datetime | None = None
    #: 验证尝试次数。冷却时间随它翻倍，所以连点会被逐步拖慢，而不是永远
    #: 十秒一次。只有验证成功才清零。
    verify_attempts: int = Field(default=0)

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

    #: 连续失败次数。用于退避：一个持续失败的账号（平台改版、账号注销）
    #: 不该每五分钟撞一次墙。任何一次成功都会清零。
    consecutive_failures: int = Field(default=0)
    rating_failures: int = Field(default=0)

    created_at: datetime = Field(default_factory=utcnow)

    user: User | None = Relationship(back_populates="accounts")

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    def verify_cooldown(self, *, base: int, cap: int) -> int:
        """下一次验证要等多少秒。

        随尝试次数翻倍：10s → 20s → 40s → … 封顶。单纯的固定冷却挡不住
        「一天到晚慢慢点」，而这个会自然把人拖停：点到第十几次时已经要
        等五分钟了。

        只有验证成功才清零，所以换一个新验证码不会重置这个计数。
        """
        if self.verify_attempts <= 0:
            return 0

        shift = min(self.verify_attempts - 1, 16)  # 防止移位数过大
        return min(base * (2**shift), cap)

    def sync_interval_seconds(self, *, now: datetime, base: int, cap: int) -> int:
        """这个账号现在该隔多久同步一次。

        间隔随「不活跃天数」翻倍，封顶 ``cap``：

            间隔 = min(cap, base × 2^不活跃天数)

        不活跃天数从 ``last_active_at``（最后一次**拓到新题**）算起；从没
        拓到过新题时改从 ``created_at`` 算，于是新账号天然落在曲线起点，
        不需要单独一个「新人档」。

        用曲线而不是固定档位的原因：档位之间的跳变很硬（一小时直接跳到
        六小时），而活跃度是连续变化的。翻倍的形状也直观——每多一天不活跃，
        间隔就翻一倍，不用记表格。

        七天封顶是个折中：再慢就没人看得到变化了。真到了谷值还想立刻更新，
        成员可以手动刷新（每周七次）。
        """
        reference = self.last_active_at or self.created_at
        idle_days = max(0.0, (now - reference).total_seconds() / DAY_SECONDS)

        # 2 ** 很大的数会溢出，先封顶再算。
        if idle_days >= 32:
            return cap
        return min(cap, int(base * (2**idle_days)))

    def is_due(self, *, now: datetime, base: int, cap: int) -> bool:
        """现在是否到了该同步的时候。

        连续失败的账号会被推后：失败一次等两倍，两次等四倍，直到封顶。
        否则一个已经注销的平台账号会每五分钟撞一次墙，白耗请求，而且日志
        里全是同一行错误。
        """
        if self.solved_fetched_at is None:
            return True

        interval = self.sync_interval_seconds(now=now, base=base, cap=cap)
        if self.consecutive_failures:
            # 2^n 增长，封顶用 cap，避免溢出。
            shift = min(self.consecutive_failures, 32)
            interval = min(cap, interval * (2**shift))

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


# --------------------------------------------------------------------------- #
# 题单
# --------------------------------------------------------------------------- #


class DailyProblemSolve(SQLModel, table=True):
    """某人某天做对了每日一题里的哪道。

    「签到」记的是这件事，不是「今天有没有做题」。两者不一样：平时刷题
    再多，没碰当天的每日一题就不算签到。

    **为什么不能靠已有数据算出来**：

    ``solved_snapshots`` 只存了「做过哪些题」，没存时间——同步时把
    ``SolvedSet.problems`` 里的时间戳丢掉了。而每日一题的题号是每天变的，
    想反查「这道题是哪天出的、谁在那天做对了」得先有题→日期的映射，
    再从提交时间倒推，绕一大圈还依赖平台给的时间精度。

    所以同步时直接比对：抓到的已过题目里，有没有当天的每日一题。有就
    落一条记录。这样签到与「题目当天出、当天做」严格对应。

    洛谷没有时间戳（``fetch_solved_luogu`` 一律给 0），它只能靠快照差分
    判断「新过了哪些题」。那种情况下也做同样的比对，只是「哪天做对的」
    精确到同步当天。
    """

    __tablename__ = "daily_problem_solves"
    __table_args__ = (
        UniqueConstraint("user_id", "day", name="uq_daily_solve_user_day"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    #: 签到日期，东八区，YYYY-MM-DD。
    day: str = Field(max_length=10, index=True)

    #: 当天做对的那道题。任意一道就算签到，记下第一道。
    platform: str = Field(max_length=16)
    key: str = Field(max_length=128)

    #: 首次记录的账号，排查用。
    account_id: int | None = Field(default=None, foreign_key="accounts.id")

    created_at: datetime = Field(default_factory=utcnow)


class ProblemCache(SQLModel, table=True):
    """题目元信息的缓存。

    题单里要显示题目名称、难度、标签，这些都得问平台。CF 一次能拉全量
    （一万多题，一个请求），洛谷和牛客得逐题问。

    所以缓存下来：CF 全量拉一次；其他平台在第一次遇到某题时抓一次，之后
    直接读。题目的名字和难度基本不会变，缓存没有过期时间。

    主键是「平台 + 题号」。题号用平台原生的写法：CF 是 ``1234A``，洛谷是
    ``P1001``，AtCoder 是 ``abc300_a``。

    牛客是例外，它有两套来源，题号里带上来源前缀区分：
    ``nowcoder:main:noob29``（主站，题号是编号）、
    ``nowcoder:contest:多组输入a+b II``（竞赛站，题号就是题名）。
    两边几乎不重合，合并起来才是完整的。
    """

    __tablename__ = "problem_cache"

    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(max_length=16, index=True)
    #: 平台原生题号。
    key: str = Field(max_length=128, index=True)

    name: str = Field(default="", max_length=300)
    #: 难度。CF 是 rating（800~3500），洛谷是 1~7 的等级，没有则为空。
    rating: int | None = None
    #: 标签，JSON 数组。各平台的标签体系不一样，直接存原文。
    tags_json: str = Field(default="[]")
    #: 题面链接。有些平台光靠题号拼不出链接，所以存下来。
    url: str = Field(default="", max_length=500)

    #: 有多少人过了。只有 CF 给（``problemStatistics`` 里带的）。
    #:
    #: 每日一题拿它当难度可信度的门槛：过的人越多，rating 越准。早年有些
    #: 题只有几十个人做，rating 是估出来的，能差好几百。
    solved_count: int | None = None

    #: 抓不到信息时记下原因，前端显示成「信息缺失」而不是空着。
    missing_reason: str = Field(default="", max_length=200)

    #: 题面是否已收录。
    #:
    #: 题面存在一份离线索引里（生成题解要用），来源是 CPRet 数据集，
    #: 只覆盖到某个时间点。CF 持续出新比赛，新题会没有题面。
    #:
    #: 每日一题抽签要跳过这些题：抽到一道没题面的题，当天就只能写「生成
    #: 失败」，与其事后报错，不如一开始不抽。
    has_statement: bool = Field(default=False)

    fetched_at: datetime = Field(default_factory=utcnow)


class DailyProblem(SQLModel, table=True):
    """每日一题。

    每天按难度分档各抽一道，档位见 ``daily.BANDS``。
    抽中过的题不再参与后续抽取，直到整池抽完——这样一轮下来覆盖面是
    均匀的，不会反复抽到那几十道「经典入门题」。

    **为什么抽中的题要落库，而不是每次按日期现算**：

    拿日期当随机种子确实能保证「同一天所有人看到同一套」，但题库会变。
    CF 时不时加新题、补 rating，题库一变，同一个日期算出来的结果就跟着
    变——昨天做的题今天再看成了另一道，这种事没法跟用户解释。

    所以抽中的那一刻就写进这张表，之后只读这份记录。种子仍然留着，用来
    复现「当时为什么抽到这道」，但不参与业务判断。

    平台先只支持 CF：rating 是官方标的，而且题库带 solvedCount，能过滤掉
    乱标难度的题（见 ``daily.py``）。其他平台等有可靠难度来源再说。
    """

    __tablename__ = "daily_problems"

    id: int | None = Field(default=None, primary_key=True)

    #: 出题日期，按工作坊时区算，格式 YYYY-MM-DD。
    day: str = Field(max_length=10, index=True)

    #: 难度档位，见 ``daily.BANDS`` 的键。
    band: str = Field(max_length=16)

    platform: str = Field(max_length=16, default="codeforces")
    key: str = Field(max_length=128, index=True)

    #: 抽签种子，排查用。
    seed: str = Field(default="", max_length=64)

    created_at: datetime = Field(default_factory=utcnow)


class EditorialStatus:
    """题解的生命周期。

    生成在凌晨四点抽完题之后，公布在晚上八点。中间那段是给人工过一眼的
    窗口——AI 写的东西偶尔会跑偏，直接对成员可见风险太大。
    """

    PENDING = "pending"  # 还没生成（或正在生成）
    DRAFT = "draft"  # 生成好了，还没到公布时间
    PUBLISHED = "published"  # 已公布
    FAILED = "failed"  # 生成失败，原因在 error 里


class Editorial(SQLModel, table=True):
    """每日一题的题解。一道题一条。

    **为什么按题存而不是按天存**：

    一天几道题，分属不同难度档。按天存的话，几篇题解挤在一个字段里，
    前端要么整块显示要么整块隐藏，没法按题展开。按题存则每道题各自
    有状态，一道生成失败不影响另外两道。

    **为什么草稿和公布分开**：

    ``status`` 和 ``published_at`` 是两个概念。生成完就是 draft，到了晚上
    八点（或管理员手动放行）才变 published。这样即使生成质量有问题，
    也能在公布前拦下来，而不是靠事后删帖。

    **hint 和题解分开存**：

    hint 是一句话提示，题解是完整推导。前端可以只显示 hint，让人自己想，
    想不出来再看题解。存成两个字段而不是混在正文里，是为了这个折叠行为
    不需要解析 markdown。
    """

    __tablename__ = "editorials"

    id: int | None = Field(default=None, primary_key=True)

    #: 对应的每日一题。一道题只有一篇题解。
    daily_problem_id: int = Field(foreign_key="daily_problems.id", index=True, unique=True)

    #: 冗余一份日期和题号，查询时不用再 join。
    day: str = Field(max_length=10, index=True)
    platform: str = Field(max_length=16, default="codeforces")
    key: str = Field(max_length=128, index=True)

    status: str = Field(default=EditorialStatus.PENDING, max_length=16, index=True)

    #: 一句话提示，按由浅入深排列。Markdown。
    hints: str = Field(default="")
    #: 完整题解。Markdown + LaTeX。
    body: str = Field(default="")

    #: 参考代码。键是语言标识（cpp20 / py3 / java17）。
    #:
    #: C++ 用 20 标准：题解里会写 ranges::sort、结构化绑定这些现代写法，
    #: 17 编译不过。字段名一度叫 code_cpp17，内容是 20，容易误导。
    code_cpp20: str = Field(default="")
    code_py3: str = Field(default="")
    code_java17: str = Field(default="")

    #: 生成用的模型，排查用。
    model: str = Field(default="", max_length=64)
    #: 失败原因，直接显示给管理员。
    error: str = Field(default="", max_length=500)

    #: 生成完成的时刻。
    generated_at: datetime | None = Field(default=None)
    #: 对成员可见的时刻。定时任务在 20:00 填，管理员也可以手动填来提前放行。
    #:
    #: 可见性判断不依赖它（见 ``editorials.public_view``）——它只是
    #: 「什么时候公布的」这条记录，以及手动提前放行的开关。
    published_at: datetime | None = Field(default=None)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProblemList(SQLModel, table=True):
    """题单，或者题单合集。

    两种东西共用一张表，用 ``kind`` 区分：

    - ``collection``：题单的集合，里面放的是别的题单。
    - ``list``：题目清单，里面放的是题目。

    分成两张表的话，查询和排序都要写两遍，而它们的字段几乎一样。用 kind
    区分更省事，代价是要在写入时保证层级正确（合集只能装题单，题单只能
    装题目），这个约束在接口层校验。

    只允许两层，合集里不能再放合集——嵌套会让完成度计算和界面都复杂很多，
    而实际用起来两层就够了。
    """

    __tablename__ = "problem_lists"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(default=ProblemListKind.LIST, max_length=16, index=True)

    title: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)

    #: 所属合集。只有 kind=list 时才有值。
    collection_id: int | None = Field(default=None, foreign_key="problem_lists.id", index=True)

    #: 排序用。小的排前面，手动拖拽时改这个值。
    position: int = Field(default=0, index=True)

    #: 只有管理员可见的内部说明。
    admin_note: str = Field(default="", max_length=500)

    created_by: int | None = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def is_collection(self) -> bool:
        return self.kind == ProblemListKind.COLLECTION


class ProblemListItem(SQLModel, table=True):
    """题单里的一道题。

    题号存平台原生写法，与 ``solved_snapshots`` 里的标识一致，这样判断
    「这个人过了没有」就是一次集合查找，不用做任何格式转换。
    """

    __tablename__ = "problem_list_items"

    id: int | None = Field(default=None, primary_key=True)
    list_id: int = Field(foreign_key="problem_lists.id", index=True)

    platform: str = Field(max_length=16)
    key: str = Field(max_length=128)

    #: 手动排序用。
    position: int = Field(default=0, index=True)

    #: 出题人想加的话可以写点提示。
    note: str = Field(default="", max_length=500)

    created_at: datetime = Field(default_factory=utcnow)
