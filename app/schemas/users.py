from pydantic import BaseModel


class UserWardResponse(BaseModel):
    ward_id: str
    name: str


class UserResponse(BaseModel):
    user_id: str
    display_name: str
    role: str
    wards: list[UserWardResponse]
