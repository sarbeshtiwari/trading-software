"""Messages HTTP boundary: no tools, no broker access, no implicit retries."""

import asyncio
import json

import httpx

from app.core.logging import register_secret
from app.llm.base import LLMProvider, ProviderFailure, ProviderReply
from app.llm.prompts import envelope

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["CONTINUE", "ABSTAIN"]},
        "rationale": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "rationale", "evidence_ids"],
    "additionalProperties": False,
}


class ClaudeProvider(LLMProvider):
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport
        if settings.anthropic_api_key is not None:
            register_secret(settings.anthropic_api_key.get_secret_value())

    def request(self, inputs, repair, *, task=None):
        return {
            "model": self.settings.llm_model,
            "max_tokens": self.settings.llm_max_output_tokens,
            "thinking": {"type": "disabled"},
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": task.output_schema if task else REVIEW_SCHEMA,
                }
            },
            **envelope(inputs, repair=repair, task=task),
        }

    async def review(self, inputs, *, repair=False, task=None) -> ProviderReply:
        if self.settings.anthropic_api_key is None:
            raise ProviderFailure("CREDENTIALS_UNAVAILABLE")
        try:
            return await asyncio.wait_for(
                self._request(self.request(inputs, repair, task=task)),
                timeout=self.settings.llm_timeout_seconds,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException) as error:
            raise ProviderFailure("TIMEOUT") from error
        except httpx.HTTPError as error:
            raise ProviderFailure("PROVIDER_ERROR") from error

    async def _request(self, payload):
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.settings.llm_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.settings.anthropic_api_key.get_secret_value(),
                    "anthropic-version": "2023-06-01",
                },
                json=payload,
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 256000:
                        raise ProviderFailure("RESPONSE_TOO_LARGE")
        if response.status_code != 200:
            return ProviderReply(
                body.decode(errors="replace"),
                outcome="RATE_LIMITED" if response.status_code == 429 else "PROVIDER_ERROR",
            )
        try:
            return self._parse(bytes(body))
        except ProviderFailure as error:
            return ProviderReply(body.decode(errors="replace"), outcome=error.code)

    def _parse(self, body):
        try:
            payload = json.loads(body)
            if payload["model"] != self.settings.llm_model:
                raise ValueError("unpriced response model")
            usage = payload["usage"]
            input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
            if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
                raise ValueError("invalid token usage")
            if input_tokens > 1000000 or output_tokens > self.settings.llm_max_output_tokens:
                raise ValueError("usage exceeds reservation")
            if any(
                usage.get(key, 0)
                for key in ("cache_creation_input_tokens", "cache_read_input_tokens")
            ):
                raise ValueError("unexpected cache usage")
            content = payload["content"]
            if payload["stop_reason"] != "end_turn" or len(content) != 1:
                return ProviderReply(body.decode(), input_tokens, output_tokens, "INVALID_RESPONSE")
            if content[0]["type"] != "text" or not isinstance(content[0]["text"], str):
                raise ValueError("unexpected content type")
            return ProviderReply(content[0]["text"], input_tokens, output_tokens)
        except (ValueError, KeyError, TypeError, IndexError, RecursionError) as error:
            raise ProviderFailure("INVALID_RESPONSE") from error
