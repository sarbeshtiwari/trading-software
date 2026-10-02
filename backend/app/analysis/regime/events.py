"""REG-006: explicit event windows with source and knowledge timestamps."""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.fno.chain.model import aware


class ScheduledEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    known_at: AwareDatetime
    start: AwareDatetime
    end: AwareDatetime
    high_impact: bool
    underlyings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_window(self):
        if self.end < self.start:
            raise ValueError("reversed event window")
        return self


class EventCalendar(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str = Field(min_length=1)
    known_at: AwareDatetime
    coverage_start: AwareDatetime
    coverage_end: AwareDatetime
    events: tuple[ScheduledEvent, ...]

    @model_validator(mode="after")
    def valid_calendar(self):
        if self.coverage_start > self.coverage_end:
            raise ValueError("reversed calendar coverage")
        if len({event.id for event in self.events}) != len(self.events):
            raise ValueError("duplicate event identity")
        if any(event.known_at > self.known_at for event in self.events):
            raise ValueError("calendar contains future knowledge")
        return self

    def active(self, underlying: str, as_of: datetime) -> tuple[str, ...] | None:
        """None means calendar unavailable; an empty tuple means known no active event."""
        aware(as_of)
        if self.known_at > as_of or not self.coverage_start <= as_of <= self.coverage_end:
            return None
        return tuple(
            sorted(
                event.id
                for event in self.events
                if event.known_at <= as_of
                and event.high_impact
                and event.start <= as_of <= event.end
                and (not event.underlyings or underlying in event.underlyings)
            )
        )
