from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from services.lottery import MODE_EVEN, NUMBER_MAX, NUMBER_MIN


class RecordIn(BaseModel):
    number: int = Field(..., ge=NUMBER_MIN, le=NUMBER_MAX, description="开奖号 1-49")


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: int
    created_at: datetime


class SettingsOut(BaseModel):
    small_max: int
    normal_max: int
    bet_unit: int
    mode: str


class SettingsPatch(BaseModel):
    small_max: int | None = Field(default=None, ge=0, le=NUMBER_MAX)
    normal_max: int | None = Field(default=None, ge=0, le=NUMBER_MAX)
    bet_unit: int | None = Field(default=None, ge=1, le=10000)
    mode: str | None = Field(default=None, pattern=f"^({MODE_EVEN}|weighted|single)$")


class RecommendRequest(BaseModel):
    mode: str | None = Field(default=None, pattern=f"^({MODE_EVEN}|weighted|single)$")
    number: int | None = Field(
        default=None, ge=NUMBER_MIN, le=NUMBER_MAX, description="临时指定最新号，不落库"
    )
