from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=1024)
    gender: str | None = Field(default=None, min_length=1, max_length=100)
    age: int | None = Field(default=None, ge=0, le=150)
    researcher_institution: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value

    @field_validator("gender", "researcher_institution", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value


class EmailRequest(BaseModel):
    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value


class LoginRequest(EmailRequest):
    password: str


class TokenRequest(BaseModel):
    refresh_token: str


class ResetConfirmRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=1024)


class VerificationConfirmRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)


class ResearcherRequest(BaseModel):
    institution: str | None = None


class CreateStudyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    duration: int = Field(gt=0)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class JoinStudyRequest(BaseModel):
    agrees_to_consent: bool


class EditStudyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str
    value: object


class SwapStudyGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    target_group: str


class QuestionnaireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: dict[str, object]


class IssueQuestionnaireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questionnaire_id: UUID


class SubmitAnswersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, object]


class FeedbackRequest(BaseModel):
    study_id: UUID | None = None
    category: str
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        if value not in {"general", "study", "technical"}:
            raise ValueError("category must be one of ['general', 'study', 'technical']")
        return value

    @model_validator(mode="after")
    def require_content(self) -> "FeedbackRequest":
        if self.rating is None and self.comment is None:
            raise ValueError("Feedback must include a rating, a comment, or both")
        return self


class LatLng(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class RouteStepInfo(BaseModel):
    index: int = Field(ge=1)
    instruction: str = ""
    start: LatLng
    end: LatLng
    duration_s: int = Field(default=0, ge=0)
    distance_m: int = Field(default=0, ge=0)
    polyline: str


class WalkRouteRequest(BaseModel):
    source: LatLng
    destination: LatLng
    total_travel_time: int = Field(ge=0, le=14400)


class ScriptWithParkRequest(BaseModel):
    source: LatLng
    destination: LatLng
    park: LatLng
    to_park_time: int = Field(ge=0, le=14400)
    park_time: int = Field(ge=0, le=14400)
    park_to_destination_time: int = Field(ge=0, le=14400)
    context: str = Field(min_length=1, max_length=1000)
    study_parameters: str = Field(default="", max_length=10000)
    source_to_park_steps: list[RouteStepInfo] = Field(default_factory=list)
    park_to_destination_steps: list[RouteStepInfo] = Field(default_factory=list)


class ScriptWithoutParkRequest(BaseModel):
    source: LatLng
    destination: LatLng
    total_walking_time: int = Field(ge=0, le=14400)
    context: str = Field(min_length=1, max_length=1000)
    study_parameters: str = Field(default="", max_length=10000)
    route_steps: list[RouteStepInfo] = Field(default_factory=list)


class TtsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=3000)


class GenerationJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["park", "without_park"]
    payload: dict[str, Any]
