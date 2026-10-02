"""Strict JSON and source membership checks; no partial-object recovery."""

import json

from app.llm.base import AdvisoryReview, LLMSchemaError


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _constant(value):
    raise ValueError("nonfinite JSON value")


def decode_json(raw):
    if len(raw.encode()) > 64000:
        raise ValueError("response too large")
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)


def parse_review(raw, inputs):
    try:
        value = decode_json(raw)
        review = AdvisoryReview.model_validate(value)
        actual = set(review.evidence_ids)
        expected = {item.source_id for item in inputs.evidence}
        if (
            len(actual) != len(review.evidence_ids)
            or not actual.issubset(expected)
            or (review.action == "CONTINUE" and (not expected or actual != expected))
            or not review.rationale.strip()
        ):
            raise ValueError("unsupported review evidence")
        return review
    except (ValueError, TypeError, RecursionError) as error:
        raise LLMSchemaError() from error
