"""Grounded notification payloads and explicit delivery outcomes."""

from pydantic import AwareDatetime, Field

from app.analysis.equity import EvidenceModel
from app.core.enums import Severity


class Notification(EvidenceModel):
    event_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=64)
    occurred_at: AwareDatetime
    severity: Severity
    message: str = Field(min_length=1, max_length=1500)
    condition_key: str | None = Field(default=None, min_length=1, max_length=128)

    def text(self):
        return (
            f"{self.severity.value}: {self.event_type}\n"
            f"Event: {self.event_id}\nTime: {self.occurred_at.isoformat()}\n{self.message}"
        )


class DeliveryPolicy(EvidenceModel):
    queue_size: int = Field(default=100, ge=1, le=10000, strict=True)
    max_attempts: int = Field(default=3, ge=1, le=5, strict=True)
    timeout_seconds: float = Field(default=10, gt=0, le=60)
    retry_delay_seconds: float = Field(default=1, ge=0, le=60)
