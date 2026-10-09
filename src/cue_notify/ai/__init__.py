"""Optional AI copy personalisation (``pip install 'cue-notify[ai]'``)."""

from __future__ import annotations

from cue_notify.ai.base import (
    ContentGenerator,
    Copy,
    GenerationError,
    PersonalizationRequest,
    PersonalizationResult,
)
from cue_notify.settings import AISettings

__all__ = [
    "ContentGenerator",
    "Copy",
    "GenerationError",
    "PersonalizationRequest",
    "PersonalizationResult",
    "create_generator",
]


def create_generator(settings: AISettings) -> ContentGenerator | None:
    """Build the configured generator, or ``None`` when AI is disabled."""
    if not settings.enabled:
        return None
    try:
        from cue_notify.ai.agent import PydanticAIGenerator
    except ImportError as exc:
        raise GenerationError(
            "AI is enabled but Pydantic AI is not installed: pip install 'cue-notify[ai]'"
        ) from exc
    return PydanticAIGenerator(settings)
