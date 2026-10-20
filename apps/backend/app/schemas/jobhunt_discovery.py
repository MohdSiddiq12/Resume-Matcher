"""Validated, read-only JobHunt discovery responses."""

import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class JobHuntJobSummary(BaseModel):
    job_key: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=500)
    company: str | None = Field(default=None, max_length=300)
    location: str | None = Field(default=None, max_length=300)
    source_url: str = Field(min_length=1, max_length=2048)
    source: str | None = Field(default=None, max_length=100)
    posted_date: str | None = None
    first_seen: str | None = None
    description_status: Literal[
        "missing", "complete", "partial", "manual", "failed", "unknown"
    ]
    description_available: bool
    pipeline_state: Literal["new", "saved", "skipped"] = "new"


class JobHuntJobPage(BaseModel):
    items: list[JobHuntJobSummary]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=20)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    offset: int = Field(default=0, ge=0, le=10_000)


class JobHuntJobDetail(JobHuntJobSummary):
    description_text: str | None = None
    description_method: str | None = None
    description_retrieved_at: str | None = None


class JobHuntTailorJobRequest(BaseModel):
    """A manually pasted full description when the source snapshot lacks one."""

    job_description: str | None = Field(default=None, min_length=50, max_length=100_000)


class JobHuntTailorJobResponse(BaseModel):
    job_key: str
    job_id: str
    created: bool
    description_status: Literal["complete", "manual"]


class JobHuntSaveResponse(BaseModel):
    job_key: str
    application_id: str
    pipeline_state: Literal["saved"] = "saved"


class JobHuntSkipResponse(BaseModel):
    job_key: str
    pipeline_state: Literal["skipped"] = "skipped"


class JobHuntLiveSearchRequest(BaseModel):
    query: str | None = Field(default=None, min_length=2, max_length=100)
    country: str | None = Field(
        default=None, min_length=2, max_length=2, pattern="^[A-Za-z]{2}$"
    )
    city: str | None = Field(default=None, min_length=1, max_length=80)
    jobage: int | None = Field(default=None, ge=1, le=90)
    include_remote: bool | None = None
    limit_per_query: int = Field(default=10, ge=1, le=20)


class JobHuntSavedSearchCreate(JobHuntLiveSearchRequest):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = False
    interval_minutes: int = Field(default=1440, ge=60, le=10_080)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class JobHuntSavedSearchUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    query: str | None = Field(default=None, min_length=2, max_length=100)
    country: str | None = Field(
        default=None, min_length=2, max_length=2, pattern="^[A-Za-z]{2}$"
    )
    city: str | None = Field(default=None, min_length=1, max_length=80)
    jobage: int | None = Field(default=None, ge=1, le=90)
    include_remote: bool | None = None
    limit_per_query: int | None = Field(default=None, ge=1, le=20)
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=60, le=10_080)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class JobHuntSavedSearchResponse(BaseModel):
    search_id: str
    name: str
    query: str | None = None
    filters: dict
    enabled: bool
    interval_minutes: int
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_run_status: str | None = None
    last_run_id: str | None = None
    created_at: str
    updated_at: str


class JobHuntSavedSearchList(BaseModel):
    items: list[JobHuntSavedSearchResponse]
    total: int = Field(ge=0)


class JobHuntLiveJobSummary(BaseModel):
    job_key: str = Field(min_length=1, max_length=256)
    canonical_id: str = Field(min_length=1, max_length=200)
    provider: str = "freehire"
    source: str | None = None
    title: str
    company: str | None = None
    location: str | None = None
    source_url: str
    posted_date: str | None = None
    description_status: Literal["missing", "complete", "partial", "manual"]
    description_available: bool
    fit_score: int | None = Field(default=None, ge=0, le=100)
    fit_evidence: dict | None = None
    salary: dict | None = None
    work_mode: str | None = None
    employment_type: str | None = None
    retrieved_at: str
    last_checked_at: str
    pipeline_state: Literal["new", "saved", "skipped"] = "new"


class JobHuntLiveJobDetail(JobHuntLiveJobSummary):
    description_text: str | None = None
    description_method: str
    description_sha256: str | None = None


class JobHuntLiveJobPage(BaseModel):
    items: list[JobHuntLiveJobSummary]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    pages: int = Field(ge=0)


class JobHuntLiveSearchResponse(BaseModel):
    run_id: str
    provider: str = "freehire"
    status: Literal["complete", "partial"]
    query_groups: list[str]
    found: int = Field(ge=0)
    new: int = Field(ge=0)
    items: list[JobHuntLiveJobSummary]
    errors: list[str] = Field(default_factory=list)


class JobHuntSearchProfile(BaseModel):
    target_titles: list[str] = Field(min_length=1, max_length=30)
    skills: list[str] = Field(default_factory=list, max_length=100)
    essential_skills: list[str] = Field(default_factory=list, max_length=30)
    irrelevant_skills: list[str] = Field(default_factory=list, max_length=100)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "skills": 0.35,
            "role": 0.3,
            "seniority": 0.15,
            "location": 0.2,
        }
    )
    country: str = Field(
        default="AE", min_length=2, max_length=2, pattern="^[A-Za-z]{2}$"
    )
    city: str = Field(default="dubai", min_length=1, max_length=80)
    jobage: int = Field(default=14, ge=1, le=90)
    include_remote: bool = True

    @field_validator("target_titles", "skills", "essential_skills", "irrelevant_skills")
    @classmethod
    def validate_terms(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value.strip()) > 120 for value in values):
            raise ValueError(
                "profile terms must be non-empty and at most 120 characters"
            )
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def validate_weights(self) -> "JobHuntSearchProfile":
        expected = {"skills", "role", "seniority", "location"}
        if set(self.weights) != expected:
            raise ValueError(
                "weights must include skills, role, seniority, and location"
            )
        if any(
            not math.isfinite(value) or value < 0 or value > 1
            for value in self.weights.values()
        ):
            raise ValueError("profile weights must be finite values from 0 to 1")
        if sum(self.weights.values()) <= 0:
            raise ValueError("at least one profile weight must be positive")
        return self
