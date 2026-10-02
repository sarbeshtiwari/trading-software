"""A reserved OpenAI slot is unavailable, never disguised as a working provider."""

from app.config import LLMProviderName
from app.llm.base import LLMProvider, ProviderFailure
from app.llm.claude import ClaudeProvider
from app.llm.fallback import DeterministicFallbackProvider


class OpenAIProvider(LLMProvider):
    async def review(self, inputs, *, repair=False, task=None):
        raise ProviderFailure("OPENAI_NOT_IMPLEMENTED")


def provider_for(settings):
    selected = settings.effective_llm_provider
    if selected == LLMProviderName.CLAUDE:
        return ClaudeProvider(settings)
    if selected == LLMProviderName.OPENAI:
        return OpenAIProvider()
    return DeterministicFallbackProvider()
