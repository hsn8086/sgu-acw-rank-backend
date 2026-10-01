"""Request and response schemas.

Kept separate from the table definitions so that a database column can never
leak into a response by accident. Anything a client may see is listed here
explicitly.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Role


class UserPublic(BaseModel):
    """What any logged-in member may see about another member."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: str
    display_name: str
    enroll_year: int
    level: int
    college_code: str
    major_code: str
    role: str
    status: str
    created_at: datetime


class UserSelf(UserPublic):
    """What a member sees about themselves. Adds nothing private for now, but
    keeps a distinct type so fields can diverge without touching callers."""


class UserAdmin(UserPublic):
    """Admin view: adds the admin-only note."""

    admin_note: str
    last_login_at: datetime | None = None


class RegisterRequest(BaseModel):
    invite_code: str = Field(min_length=1, max_length=32)
    student_id: str = Field(min_length=1, max_length=16)
    display_name: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("姓名不能为空")
        return cleaned


class LoginRequest(BaseModel):
    student_id: str = Field(min_length=1, max_length=16)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class UpdateSelfRequest(BaseModel):
    """Fields a member may change about themselves.

    Student id, role and status are deliberately absent: the first is the login
    identity, the other two are administrative.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("姓名不能为空")
        return cleaned


class InviteCreate(BaseModel):
    max_uses: int = Field(default=1, ge=1, le=500)
    ttl_hours: int = Field(default=24, ge=1, le=24 * 365)
    grants_role: str = Field(default=Role.MEMBER)
    note: str = Field(default="", max_length=500)

    @field_validator("grants_role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        if value not in Role.ALL:
            raise ValueError("未知的角色")
        return value


class InvitePublic(BaseModel):
    """Invite as shown to administrators."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    grants_role: str
    max_uses: int
    use_count: int
    expires_at: datetime
    revoked_at: datetime | None
    note: str
    created_by: int | None
    created_at: datetime
    is_usable: bool


class UpdateUserAdminRequest(BaseModel):
    """Administrative edits to another member.

    ``admin_note`` is the admin-only field; an empty string clears it. Sending
    ``None`` leaves it untouched.

    学号不在可改字段里：它是登录名，改它等于换了个账号。真需要纠正时走
    单独的接口，那里会一并检查是否与现有学号冲突。
    """

    role: str | None = None
    status: str | None = None
    admin_note: str | None = Field(default=None, max_length=2000)
    display_name: str | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str | None) -> str | None:
        if value is not None and value not in Role.ALL:
            raise ValueError("未知的角色")
        return value

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str | None) -> str | None:
        if value is not None and value not in ("active", "disabled"):
            raise ValueError("未知的状态")
        return value

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("姓名不能为空")
        return cleaned


class AdminCreateUserRequest(BaseModel):
    """管理员直接建号，不需要邀请码。

    用于代那些不自己注册的人建号，比如已经毕业但还在榜上的成员。
    """

    student_id: str = Field(min_length=1, max_length=16)
    display_name: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default=Role.MEMBER)
    admin_note: str = Field(default="", max_length=2000)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("姓名不能为空")
        return cleaned

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        if value not in Role.ALL:
            raise ValueError("未知的角色")
        return value


class AdminUserPage(BaseModel):
    """成员列表的一页。带上总数与筛选后的数量，供分页控件使用。"""

    items: list[UserAdmin]
    total: int
    page: int
    page_size: int
    #: 可选值，让前端不必写死年级下拉的选项。
    enroll_years: list[int]
    college_codes: list[str]


class AccountPublic(BaseModel):
    """一个平台账号。未验证的也会返回，好让成员看到待办状态。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    identifier: str
    verified_at: datetime | None
    #: 仅在未验证时出现，用于告诉成员该把什么填进签名。
    verify_token: str | None = None
    verify_expires_at: datetime | None = None
    created_at: datetime


class AccountCreate(BaseModel):
    platform: str = Field(min_length=1, max_length=16)
    #: Codeforces/AtCoder 的 handle，或洛谷/牛客的 uid。洛谷也接受用户名。
    identifier: str = Field(min_length=1, max_length=64)


class AccountVerifyResult(BaseModel):
    ok: bool
    detail: str
    account: AccountPublic | None = None


class RankEntry(BaseModel):
    """榜单的一行。一个人可能有多个账号，所以以账号为单位而非以人为单位。"""

    user_id: int
    display_name: str
    student_id: str
    enroll_year: int
    platform: str
    identifier: str
    rank: int = 0

    rating: int | None = None
    max_rating: int | None = None
    #: 平台上的显示名，可能与 identifier 不同。
    platform_name: str | None = None
    #: 抓取失败的原因。有值时其余数据为空，界面照常显示这一行但标注异常。
    error: str | None = None


class RankResponse(BaseModel):
    platform: str
    entries: list[RankEntry]
    fetched_at: int
    cache_ttl: int


class PracticeDay(BaseModel):
    date: str
    count: int


class DailyPracticeEntry(BaseModel):
    """每日练习榜的一行，以人为单位（同平台多个账号已合并）。"""

    user_id: int
    display_name: str
    student_id: str
    enroll_year: int
    rank: int = 0

    #: 统计窗口内的过题总数。
    total: int = 0
    #: 窗口内每一天的数量，顺序与 dates 一致，缺失的补 0。
    days: list[PracticeDay] = Field(default_factory=list)


class DailyPracticeResponse(BaseModel):
    days: int
    platform: str | None
    #: 窗口内的日期，从早到晚。
    dates: list[str]
    entries: list[DailyPracticeEntry]
    generated_at: int


class SyncStatus(BaseModel):
    has_run: bool
    started_at: datetime | None = None
    finished_at: datetime | None = None
    account_count: int = 0
    ok_count: int = 0
    error_count: int = 0
    #: 失败账号的说明，供管理员排查。
    errors: list[str] = Field(default_factory=list)
