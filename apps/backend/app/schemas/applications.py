"""Pydantic schemas for the Kanban application tracker."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic.json_schema import SkipJsonSchema


class ApplicationStatus(str, Enum):
    """The seven stable tracker columns (decoupled from i18n labels)."""

    saved = "saved"
    applied = "applied"
    no_response = "no_response"
    response = "response"
    interview = "interview"
    accepted = "accepted"
    rejected = "rejected"


# Order the board renders columns in.
APPLICATION_STATUS_ORDER: list[str] = [s.value for s in ApplicationStatus]


class ApplicationResponse(BaseModel):
    """A single tracker card."""

    application_id: str
    job_id: str
    resume_id: str | None = None
    master_resume_id: str | None = None
    status: ApplicationStatus
    company: str | None = None
    role: str | None = None
    applied_at: str | None = None
    notes: str | None = None
    position: int
    created_at: str
    updated_at: str
    legacy_application_id: str | None = None
    legacy_status: str | None = None
    follow_up_date: str | None = None


class ApplicationDetailResponse(ApplicationResponse):
    """A card plus the embedded job description and applied resume.

    ``resume`` is null when the referenced resume has been deleted — the modal
    renders "resume unavailable" rather than 500ing.
    """

    job_content: str | None = None
    resume: dict[str, Any] | None = None


class ApplicationListResponse(BaseModel):
    """Applications grouped by column. All seven keys are always present."""

    columns: dict[str, list[ApplicationResponse]]


class ManualApplicationCreate(BaseModel):
    """Create a card from a pasted JD (no prior tailoring).

    The router creates the job from ``job_description`` then the application.
    ``company``/``role`` are optional overrides; when omitted the router runs a
    best-effort extraction.
    """

    resume_id: str
    job_description: str = Field(min_length=1)
    company: str | None = None
    role: str | None = None
    status: ApplicationStatus = ApplicationStatus.saved
    confirmed: bool | None = None
    notes: str | None = None


class TrackDiscoveryJobRequest(BaseModel):
    """Request to track a job discovered from the snapshot into the saved column."""

    job_key: str = Field(min_length=1, max_length=256)


class ApplicationUpdate(BaseModel):
    """Partial update — every field optional."""

    status: ApplicationStatus | SkipJsonSchema[None] = None
    position: int | None = None
    notes: str | None = None
    company: str | None = None
    role: str | None = None
    applied_at: str | None = None
    follow_up_date: str | None = None
    confirmed: bool | None = None
    note: str | None = None

    @field_validator("status")
    @classmethod
    def reject_null_status(cls, value: ApplicationStatus | None) -> ApplicationStatus:
        """Omission preserves status; an explicitly supplied null is invalid."""
        if value is None:
            raise ValueError("Status cannot be null")
        return value

    @field_validator("follow_up_date")
    @classmethod
    def validate_follow_up_date(cls, value: str | None) -> str | None:
        """Validate follow-up date as YYYY-MM-DD format."""
        if value in (None, ""):
            return None
        from datetime import date

        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("follow_up_date must be a valid date in YYYY-MM-DD format")
        return value


class BulkStatusUpdate(BaseModel):
    """Move many cards to one column."""

    application_ids: list[str] = Field(min_length=1)
    status: ApplicationStatus
    confirmed: bool | None = None


class BulkDelete(BaseModel):
    """Delete many cards."""

    application_ids: list[str] = Field(min_length=1)


class ApplicationActionResponse(BaseModel):
    """Generic acknowledgement for bulk/destructive actions."""

    message: str
    affected: int


class ApplicationEventResponse(BaseModel):
    """An audit trail event for an application status transition."""

    event_id: str
    application_id: str
    from_status: str | None = None
    to_status: str
    confirmed: bool
    note: str | None = None
    created_at: str


class ApplicationEventListResponse(BaseModel):
    items: list[ApplicationEventResponse]


class ApplicationOutreachDraftRequest(BaseModel):
    content: str = Field(max_length=10_000)


class ApplicationOutreachDraftResponse(BaseModel):
    application_id: str
    content: str
    updated_at: str | None = None
