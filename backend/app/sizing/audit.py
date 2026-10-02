"""SIZE-008: immutable full-precision inputs and deterministic replay."""

from app.db import session as db_session
from app.db.models.decision import SizingRecord
from app.sizing.models import SizingInputs, SizingPolicy, SizingResult
from app.sizing.risk_based import size_position


class SizingAudit:
    async def calculate_and_record(
        self, inputs: SizingInputs, policy: SizingPolicy
    ) -> tuple[str, SizingResult]:
        result = size_position(inputs, policy)
        async with db_session.session_scope() as session:
            row = SizingRecord(
                proposal_id=inputs.proposal_id,
                method="RISK_BASED",
                formula_version=result.formula_version,
                capital=result.capital,
                risk_budget=result.risk_budget,
                risk_per_unit=result.risk_per_unit,
                raw_quantity=result.raw_quantity,
                lot_size=inputs.lot_size,
                final_quantity=result.quantity,
                binding_constraint=result.binding_constraint,
                zero_reason=result.zero_reason,
                inputs={
                    "request": inputs.model_dump(mode="json"),
                    "policy": policy.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                },
            )
            session.add(row)
            await session.flush()
            return row.id, result

    async def replay(self, identifier: str) -> SizingResult:
        async with db_session.session_scope() as session:
            row = await session.get(SizingRecord, identifier)
            if row is None:
                raise ValueError("SIZING_RECORD_UNAVAILABLE")
            if row.formula_version not in {"1.0.0", "1.1.0"}:
                raise ValueError("UNSUPPORTED_SIZING_FORMULA")
            result = size_position(
                SizingInputs.model_validate(row.inputs["request"]),
                SizingPolicy.model_validate(row.inputs["policy"]),
            )
            if (
                result.formula_version != row.formula_version
                or result != SizingResult.model_validate(row.inputs["result"])
                or result.quantity != row.final_quantity
                or row.method != "RISK_BASED"
            ):
                raise ValueError("SIZING_REPLAY_MISMATCH")
            return result
