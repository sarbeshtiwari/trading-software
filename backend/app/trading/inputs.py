"""Server-side publication of explicit, attributable reference decision inputs."""

import sqlalchemy as sa
from pydantic import PrivateAttr, model_validator

from app.agents.pipeline import ContractCosts
from app.agents.validation import ValidationPolicy
from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import get_clock
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.modes import TradingMode
from app.trading.contracts import CashContractSource, LongOptionContractSource
from app.trading.regime import RegimeSource


class ReferenceInputs(EvidenceModel):
    costs: ContractCosts | None = None
    contract_source: LongOptionContractSource | CashContractSource | None = None
    validation: ValidationPolicy
    ban_listed: bool
    news_halt: bool
    manually_blocked: bool
    regime_source: RegimeSource | None = None
    _publication_id: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def one_contract_source(self):
        if (self.costs is None) == (self.contract_source is None):
            raise ValueError("exactly one contract observation or producer policy is required")
        return self

    @property
    def publication_id(self):
        return self._publication_id

    @property
    def instrument_id(self):
        return (self.costs or self.contract_source).instrument_id

    @property
    def data_origin(self):
        return (self.costs or self.contract_source).data_origin

    def require_known(self, as_of):
        if self.costs is not None:
            if not self.costs.observed_at <= self.costs.available_at <= as_of:
                raise ValueError("future reference input")
        elif self.contract_source.known_at > as_of:
            raise ValueError("future contract policy knowledge")


class ReferenceInputStore:
    def __init__(self, clock=None):
        self.clock = clock or get_clock()

    async def publish(
        self, inputs: ReferenceInputs, *, actor: str, reason: str = "Source publication"
    ):
        inputs = ReferenceInputs.model_validate(inputs.model_dump())
        inputs.require_known(self.clock.now())
        if inputs.regime_source is not None:
            inputs.regime_source.require_known(self.clock.now())
        identifier = new_id("rin")
        await AuditService(self.clock).append(
            AuditIdentity(
                chain_id=identifier,
                event_type="REFERENCE_INPUTS",
                actor=actor,
                mode=TradingMode.PAPER,
            ),
            {
                "instrument_id": inputs.instrument_id,
                "data_used": inputs.model_dump(mode="json"),
                "result": {"reason": reason},
            },
        )
        return identifier

    async def at(self, instrument_id, origin, as_of):
        async with db_session.session_scope() as session:
            rows = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(
                            AuditEvent.event_type == "REFERENCE_INPUTS",
                            AuditEvent.instrument_id == instrument_id,
                            AuditEvent.occurred_at <= as_of,
                        )
                        .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                    )
                ).all()
            )
        for row in rows:
            if not await AuditService(self.clock).verify(row.chain_id, expected_count=1):
                raise ValueError("reference inputs audit integrity failure")
            inputs = ReferenceInputs.model_validate(row.data_used)
            if inputs.data_origin == origin:
                inputs.require_known(as_of)
                if inputs.regime_source is not None:
                    inputs.regime_source.require_known(as_of)
                inputs._publication_id = row.chain_id
                return inputs
        return None
