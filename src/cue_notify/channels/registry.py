"""Builds configured channel instances from settings and installed providers."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator, Mapping
from importlib.metadata import entry_points
from typing import Any

import httpx
from pydantic import ValidationError

from cue_notify.channels.base import Channel
from cue_notify.settings import ChannelSpec

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "cue.channels"

type AnyChannel = Channel[Any]


class ChannelConfigError(ValueError):
    pass


BUILTIN_PROVIDERS = {
    "console": "cue_notify.channels.console:ConsoleChannel",
    "webhook": "cue_notify.channels.webhook:WebhookChannel",
    "fcm": "cue_notify.channels.fcm:FcmChannel",
    "twilio": "cue_notify.channels.twilio:TwilioChannel",
    "smtp": "cue_notify.channels.smtp:SmtpChannel",
    "telegram": "cue_notify.channels.telegram:TelegramChannel",
    "slack": "cue_notify.channels.slack:SlackChannel",
    "resend": "cue_notify.channels.resend:ResendChannel",
    "ses": "cue_notify.channels.ses:SesChannel",
    "apns": "cue_notify.channels.apns:ApnsChannel",
}


def _load(target: str) -> object:
    module, _, attribute = target.partition(":")
    return getattr(importlib.import_module(module), attribute)


def discover_providers() -> dict[str, type[AnyChannel]]:
    """Return ``{provider_name: channel_class}`` for built-in and plugin providers.

    Plugins registered under the ``cue.channels`` entry-point group may override
    built-ins of the same name.
    """
    targets: dict[str, str] = dict(BUILTIN_PROVIDERS)
    targets.update({entry.name: entry.value for entry in entry_points(group=ENTRY_POINT_GROUP)})
    providers: dict[str, type[AnyChannel]] = {}
    for name, target in targets.items():
        try:
            cls = _load(target)
        except Exception:  # a broken plugin must not take the service down
            logger.exception("failed to load channel provider %r from %s", name, target)
            continue
        if not (isinstance(cls, type) and issubclass(cls, Channel)):
            logger.error("channel provider %r (%s) is not a Channel subclass", name, target)
            continue
        providers[name] = cls
    return providers


class ChannelRegistry(Mapping[str, AnyChannel]):
    """Immutable mapping of channel name → configured channel instance."""

    def __init__(self, channels: Mapping[str, AnyChannel]) -> None:
        self._channels = dict(channels)

    @classmethod
    def from_settings(
        cls,
        specs: Mapping[str, ChannelSpec],
        http: httpx.AsyncClient,
        providers: Mapping[str, type[AnyChannel]] | None = None,
    ) -> ChannelRegistry:
        available = dict(providers) if providers is not None else discover_providers()
        channels: dict[str, AnyChannel] = {}
        for name, spec in specs.items():
            if not spec.enabled:
                continue
            provider = available.get(spec.provider)
            if provider is None:
                known = ", ".join(sorted(available)) or "none"
                raise ChannelConfigError(
                    f"channel {name!r}: unknown provider {spec.provider!r} (installed: {known})"
                )
            try:
                config = provider.config_model.model_validate(spec.options)
                channels[name] = provider(name, config, http)
            except ValidationError as exc:
                # Without the input values (and without chaining the original), so channel
                # credentials never end up in startup logs.
                problems = "; ".join(
                    f"{'.'.join(map(str, error['loc'])) or 'config'}: {error['msg']}"
                    for error in exc.errors(include_input=False, include_url=False)
                )
                raise ChannelConfigError(
                    f"channel {name!r} ({spec.provider}): {problems}"
                ) from None
            except (OSError, ValueError) as exc:
                raise ChannelConfigError(f"channel {name!r} ({spec.provider}): {exc}") from exc
        return cls(channels)

    def __getitem__(self, name: str) -> AnyChannel:
        return self._channels[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._channels)

    def __len__(self) -> int:
        return len(self._channels)

    async def aclose(self) -> None:
        for channel in self._channels.values():
            await channel.aclose()
