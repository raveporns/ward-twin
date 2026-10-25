from typing import Annotated

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    ConfigDict,
    StringConstraints,
)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr

    password: str = Field(
        min_length=12,
        max_length=128,
    )

    display_name: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=100,
        ),
    ]


class RegisterResponse(BaseModel):
    user_id: str
    email: EmailStr
    display_name: str
    role: str
    status: str

class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int

class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ApproveAccountRequest(BaseModel):
    role: Literal["viewer", "nurse", "doctor"]

    ward_ids: list[str] = Field(
        min_length=1,
        max_length=20,
    )

    @field_validator("ward_ids")
    @classmethod
    def validate_ward_ids(cls, values: list[str]) -> list[str]:
        normalized = []

        for ward_id in values:
            ward_id = ward_id.strip()

            if not ward_id:
                raise ValueError("Ward ID cannot be empty")

            if ward_id in normalized:
                raise ValueError("Duplicate Ward ID")

            normalized.append(ward_id)

        return normalized



class ApproveAccountResponse(BaseModel):
    user_id: str
    role: str
    status: Literal["approved"]
    ward_ids: list[str]
    reviewed_by: str
