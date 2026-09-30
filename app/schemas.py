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
    """

    role: str | None = None
    status: str | None = None
    admin_note: str | None = Field(default=None, max_length=2000)

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
