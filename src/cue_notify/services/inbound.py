"""Replies to text messages: STOP and START.

The FCC treats "stop", "quit", "end", "revoke", "opt out", "cancel" and "unsubscribe" as
a revocation of consent that must be honoured (47 CFR 64.1200(a)(10)). A reply with one
of them mutes text messages for everyone with that number and unsubscribes them from
every optional category, in the consent ledger with source ``sms_keyword``. START puts
text messages back, but not the categories: those need a fresh opt-in.

Providers that answer STOP themselves (Twilio's Advanced Opt-Out, for instance) still
need Cue to know, or it keeps planning messages the provider will refuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.core.enums import PreferenceSource
from cue_notify.db.models import Address, Category, Recipient
from cue_notify.runtime import Runtime
from cue_notify.services import preferences
from cue_notify.services import recipients as recipient_service

OPT_OUT = frozenset(
    {"stop", "stopall", "quit", "end", "revoke", "opt out", "optout", "cancel", "unsubscribe"}
)
OPT_IN = frozenset({"start", "unstop", "yes"})

Action = Literal["opt_out", "opt_in"]


def keyword(body: str) -> Action | None:
    """The action a reply asks for, if it is one of the recognised keywords."""
    word = " ".join(body.lower().split()).strip(" .!")
    if word in OPT_OUT:
        return "opt_out"
    if word in OPT_IN:
        return "opt_in"
    return None


@dataclass
class InboundResult:
    action: Action | None
    recipients: list[str] = field(default_factory=list)


async def handle(
    session: AsyncSession,
    runtime: Runtime,
    phone: str,
    body: str,
    provenance: dict[str, Any],
) -> InboundResult:
    action = keyword(body)
    result = InboundResult(action)
    if action is None:
        return result
    sms = runtime.settings.compliance.sms_channels
    owners = await session.scalars(
        select(Address.recipient_id)
        .where(Address.channel.in_(sms), Address.value == phone.replace(" ", ""))
        .distinct()
    )
    optional = {
        c.key for c in await session.scalars(select(Category).where(Category.allow_unsubscribe))
    }
    for recipient_id in list(owners):
        recipient = await session.get(Recipient, recipient_id, with_for_update=True)
        if recipient is None:
            continue
        prefs = recipient_service.preferences(recipient)
        if action == "opt_out":
            updated = prefs.model_copy(
                update={
                    "muted_channels": prefs.muted_channels | set(sms),
                    "unsubscribed_categories": prefs.unsubscribed_categories | optional,
                }
            )
        else:
            updated = prefs.model_copy(update={"muted_channels": prefs.muted_channels - set(sms)})
        await preferences.apply(
            session,
            recipient,
            updated,
            PreferenceSource.SMS_KEYWORD,
            {**provenance, "keyword": " ".join(body.split())[:40], "from": phone},
        )
        result.recipients.append(recipient.external_id)
    return result
