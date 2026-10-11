"""Live checks against TypeSafe's API. Skipped unless ``TYPESAFE_API_KEY`` is set.

Run by hand through the "Live checks" workflow; each run costs well under a cent.
"""

from __future__ import annotations

import os

import httpx
import pytest

from cue_notify.ai.screening import ScreeningRequest, TypeSafeScreener
from cue_notify.core.enums import Importance
from cue_notify.settings import ScreeningSettings

pytestmark = pytest.mark.skipif(
    not os.environ.get("TYPESAFE_API_KEY"), reason="TYPESAFE_API_KEY is not set"
)


def _request(title: str, body: str, importance: Importance) -> ScreeningRequest:
    return ScreeningRequest(
        agent="support-bot",
        agent_description="Answers customer support tickets for an online shop",
        category="transactional",
        importance=importance,
        content={"default": {"title": title, "body": body}},
    )


async def test_jev_screens_real_messages() -> None:
    settings = ScreeningSettings(enabled=True, timeout=20)
    async with httpx.AsyncClient() as http:
        screener = TypeSafeScreener(settings, http)
        routine = await screener.screen(
            _request(
                "Your order has shipped",
                "Order A-1042 left our warehouse today and should arrive on Thursday.",
                Importance.NORMAL,
            )
        )
        phishing = await screener.screen(
            _request(
                "Final warning: account suspended",
                "Your account will be deleted in 1 hour. Confirm your password and card "
                "number at http://shop-verify.example to keep it.",
                Importance.HIGH,
            )
        )
    print("routine:", routine.record)
    print("phishing:", phishing.record)
    assert routine.hold is None
    assert phishing.hold is not None
    assert "deception" in phishing.hold
