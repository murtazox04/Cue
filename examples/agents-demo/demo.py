from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

PAUSE = float(os.environ.get("CUE_DEMO_PAUSE", "1"))
COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
CITIES = (
    "Pacific/Honolulu",
    "America/Anchorage",
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Sao_Paulo",
    "Atlantic/Azores",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Istanbul",
    "Asia/Dubai",
    "Asia/Tashkent",
    "Asia/Dhaka",
    "Asia/Bangkok",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Pacific/Auckland",
)
CONSOLE_LINE = re.compile(r"cue_notify\.channels\.console: (.*?)(?: message_id=\S+)?$")
STATUS_COLORS = {
    "sent": "32",
    "queued": "33",
    "suppressed": "31",
    "pending_approval": "35",
    "digested": "36",
}


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def say(text: str = "", pause: float = 0.0) -> None:
    print(text, flush=True)
    time.sleep(pause * PAUSE)


def heading(text: str) -> None:
    say()
    say(paint(text, "1"), 1.0)


def row(status: str, subject: str, note: str = "") -> None:
    label = paint(f"{status:<17}", STATUS_COLORS.get(status, "0"))
    say(f"  {label}{subject:<27}{paint(note, '2')}", 0.6)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def night_zone(now: datetime) -> str:
    def distance_from_two_am(zone: str) -> float:
        local = now.astimezone(ZoneInfo(zone))
        hours = local.hour + local.minute / 60
        return min(abs(hours - 2), 24 - abs(hours - 2))

    return min(CITIES, key=distance_from_two_am)


class Cue:
    def __init__(self, workdir: Path) -> None:
        port = free_port()
        self.url = f"http://127.0.0.1:{port}"
        self.log = workdir / "cue.log"
        self.offset = 0
        env = {key: value for key, value in os.environ.items() if not key.startswith("CUE_")}
        env |= {
            "PORT": str(port),
            "CUE_DATABASE__URL": f"sqlite+aiosqlite:///{workdir / 'cue.db'}",
            "CUE_WORKER__EMBEDDED": "true",
            "CUE_WORKER__POLL_INTERVAL": "0.2",
            "CUE_CHANNELS__PUSH__PROVIDER": "console",
            "CUE_API__PUBLIC_URL": self.url,
            "CUE_API__SECRET_KEY": secrets.token_urlsafe(32),
        }
        self.output = self.log.open("wb")
        self.admin = httpx.Client(base_url=self.url)
        self.server = subprocess.Popen(
            [sys.executable, "-m", "cue_notify", "serve", "--migrate"],
            cwd=workdir,
            env=env,
            stdout=self.output,
            stderr=subprocess.STDOUT,
        )
        self.workdir = workdir
        self.env = env

    def start(self) -> None:
        until(self.ready, timeout=60)
        created = subprocess.run(
            [sys.executable, "-m", "cue_notify", "keys", "create", "admin"],
            cwd=self.workdir,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.admin.headers["Authorization"] = f"Bearer {created.stdout.strip()}"

    def ready(self) -> bool:
        if self.server.poll() is not None:
            raise RuntimeError(f"Cue exited; see its log:\n{self.log.read_text()}")
        try:
            return httpx.get(f"{self.url}/readyz").status_code == 200
        except httpx.TransportError:
            return False

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.admin.post(path, json=body)
        response.raise_for_status()
        return response.json()

    def get(self, path: str, **params: str) -> dict[str, Any]:
        response = self.admin.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def agent_client(self, key: str) -> httpx.Client:
        return httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {key}"})

    def console(self) -> list[str]:
        with self.log.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read()
        complete = chunk[: chunk.rfind(b"\n") + 1]
        self.offset += len(complete)
        lines = complete.decode("utf-8", errors="replace").splitlines()
        return [found.group(1) for found in map(CONSOLE_LINE.search, lines) if found]

    def show_console(self, expected: int) -> None:
        shown = 0
        deadline = time.monotonic() + 15
        while shown < expected and time.monotonic() < deadline:
            for line in self.console():
                shown += 1
                say(f"  {paint('console │', '36')} {line}", 0.5)
            time.sleep(0.1)

    def close(self) -> None:
        self.admin.close()
        self.server.terminate()
        self.server.wait(timeout=15)
        self.output.close()


def until(check: Callable[[], Any], timeout: float = 15) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError("Cue did not reach the expected state in time")


@contextmanager
def running_cue() -> Iterator[Cue]:
    with tempfile.TemporaryDirectory(prefix="cue-agents-demo-") as workdir:
        cue = Cue(Path(workdir))
        try:
            cue.start()
            yield cue
        finally:
            cue.close()


def local_time(value: str, zone: str) -> str:
    return datetime.fromisoformat(value).astimezone(ZoneInfo(zone)).strftime("%H:%M")


def deferred(message: dict[str, Any]) -> bool:
    return datetime.fromisoformat(message["send_at"]) > datetime.now(UTC)


def configure(cue: Cue, zone: str) -> dict[str, str]:
    for category in (
        {"key": "incidents", "respect_quiet_hours": False, "allow_unsubscribe": False},
        {"key": "dev-updates", "respect_quiet_hours": True},
    ):
        cue.post("/v1/categories", category)
    for template in (
        {
            "key": "alert",
            "locales": {"en": {"title": "{{ data.title }}", "body": "{{ data.body }}"}},
        },
        {
            "key": "review-digest",
            "locales": {
                "en": {
                    "title": "{{ data.count }} code reviews finished",
                    "body": "{% for r in data.events %}#{{ r.pr }} {{ r.verdict }}"
                    "{% if not loop.last %}, {% endif %}{% endfor %}",
                }
            },
        },
        {
            "key": "review-request",
            "locales": {
                "en": {
                    "title": "{{ data.agent_name }} needs your OK",
                    "body": "{{ data.title }}",
                    "url": "{{ data.review_url }}",
                }
            },
        },
    ):
        cue.post("/v1/templates", template)
    cue.post(
        "/v1/rules",
        {
            "key": "review-digest",
            "name": "Batch finished reviews",
            "event": "review.finished",
            "template": "review-digest",
            "category": "dev-updates",
            "channels": ["push"],
            "digest": {"window": "PT1H", "send_after": 4},
        },
    )
    cue.post(
        "/v1/rules",
        {
            "key": "review-requests",
            "name": "Ask a reviewer",
            "event": "cue.approval_requested",
            "template": "review-request",
            "category": "transactional",
            "channels": ["push"],
        },
    )
    for person, device in (("dana", "dana-phone"), ("lead", "lead-phone")):
        response = cue.admin.put(
            f"/v1/recipients/{person}",
            json={"timezone": zone, "addresses": [{"channel": "push", "value": device}]},
        )
        response.raise_for_status()
    agents = (
        {
            "key": "triage-bot",
            "name": "triage-bot",
            "categories": ["incidents"],
            "max_importance": "high",
            "frequency_caps": [{"period": "PT1H", "limit": 3}],
        },
        {"key": "review-bot", "name": "review-bot", "categories": ["dev-updates"]},
        {
            "key": "billing-bot",
            "name": "billing-bot",
            "categories": ["incidents", "dev-updates"],
            "approval": {"in": [{"var": "message.importance"}, ["high", "critical"]]},
            "reviewers": ["lead"],
        },
    )
    keys = {}
    for agent in agents:
        cue.post("/v1/agents", agent)
        created = cue.post(
            "/v1/api-keys",
            {
                "name": f"{agent['key']}-key",
                "agent": agent["key"],
                "scopes": ["events:write", "messages:write", "messages:read"],
            },
        )
        keys[agent["key"]] = created["key"]
    return keys


def send(client: httpx.Client, category: str, importance: str, title: str, body: str) -> Any:
    response = client.post(
        "/v1/messages",
        json={
            "recipient": "dana",
            "template": "alert",
            "category": category,
            "channels": ["push"],
            "importance": importance,
            "data": {"title": title, "body": body},
        },
    )
    response.raise_for_status()
    return response.json()


def triage(cue: Cue, key: str) -> None:
    heading("1  triage-bot sends 5 alerts  (budget: 3 per hour per person, importance <= high)")
    alerts = (
        ("high", "api: 5xx rate 4.2%", "threshold 2%"),
        ("critical", "db-2: disk 91% full", "grows 3%/h"),
        ("high", "worker: queue lag 340 s", "3 consumers down"),
        ("high", "api: 5xx rate 4.6%", "still rising"),
        ("normal", "cache: hit ratio 61%", "was 94%"),
    )
    with cue.agent_client(key) as client:
        for asked, title, body in alerts:
            message = send(client, "incidents", asked, title, body)
            if message["status"] == "suppressed":
                note = message["status_reason"]
            elif message["importance"] != asked:
                note = f"asked {asked}, capped at {message['importance']}"
            else:
                note = message["importance"]
            row(message["status"], title, note)
    cue.show_console(3)


def reviews(cue: Cue, key: str, zone: str) -> None:
    heading("2  review-bot reports 4 finished reviews  (a digest rule batches them)")
    verdicts = (("412", "approved"), ("415", "changes requested"), ("418", "approved"))
    verdicts += (("421", "approved"),)
    with cue.agent_client(key) as client:
        for number, verdict in verdicts:
            response = client.post(
                "/v1/events",
                json={
                    "name": "review.finished",
                    "recipient": "dana",
                    "data": {"pr": number, "verdict": verdict},
                },
            )
            response.raise_for_status()
            event_id = response.json()["id"]
            event = until(lambda event_id=event_id: processed(cue, event_id))
            row(event["outcome"][0]["reason"], f"review.finished #{number}", verdict)
    digest_id = event["outcome"][0]["digest_id"]
    digest = until(lambda: sent_digest(cue, digest_id))
    message = cue.get(f"/v1/messages/{digest['message_id']}")
    title = message["content"]["default"]["title"]
    when = local_time(message["send_at"], zone)
    note = f"one digest; quiet hours: held to {when}" if deferred(message) else "one digest"
    row(message["status"], title, note)


def processed(cue: Cue, event_id: str) -> dict[str, Any] | None:
    event = cue.get(f"/v1/events/{event_id}")
    return event if event["status"] == "processed" else None


def sent_digest(cue: Cue, digest_id: str) -> dict[str, Any] | None:
    digest = cue.get(f"/v1/digests/{digest_id}")
    return digest if digest["status"] == "sent" else None


def billing(cue: Cue, key: str, zone: str) -> None:
    heading("3  billing-bot sends 2 messages  (high importance needs lead's approval)")
    with cue.agent_client(key) as client:
        summary = send(client, "dev-updates", "normal", "Invoice run finished", "1,284 sent")
        when = local_time(summary["send_at"], zone)
        note = f"quiet hours: held to {when}" if deferred(summary) else ""
        row(summary["status"], "Invoice run finished", note)
        urgent = send(
            client, "incidents", "high", "Stripe webhooks failing", "38 payments unconfirmed"
        )
        row(urgent["status"], "Stripe webhooks failing", "approval rule: importance high")
    cue.show_console(1)
    request = until(lambda: review_request(cue))
    link = request["content"]["default"]["url"]
    short = link.replace(cue.url, "")[:20] + "…"
    say(f"  {paint('lead', '1')} opens the link from that notification and approves:", 0.4)
    with httpx.Client(base_url=cue.url) as browser:
        page = browser.get(link)
        heading_text = re.search(r"<h1>(.*?)</h1>", page.text)
        shown = heading_text.group(1) if heading_text else ""
        say(f"  GET  {short}  {page.status_code}  {shown}", 0.5)
        decided = browser.post(link, data={"action": "approve"})
        notice = re.search(r'role="status">(.*?)</p>', decided.text)
        result = notice.group(1) if notice else str(decided.status_code)
        say(f"  POST {short}  action=approve  {paint(result, '32')}", 0.4)
    cue.show_console(1)
    approved = until(lambda: delivered(cue, urgent["id"]))
    row(approved["status"], "Stripe webhooks failing", f"reviewed_by {approved['reviewed_by']}")


def review_request(cue: Cue) -> dict[str, Any] | None:
    items = cue.get("/v1/messages", recipient="lead")["items"]
    return items[0] if items else None


def delivered(cue: Cue, message_id: str) -> dict[str, Any] | None:
    message = cue.get(f"/v1/messages/{message_id}")
    return message if message["status"] == "sent" else None


def summary(cue: Cue, zone: str) -> None:
    heading("What reached dana  (GET /v1/messages?recipient=dana)")
    items = cue.get("/v1/messages", recipient="dana", limit="50")["items"]
    counts: dict[str, int] = {}
    for message in items:
        counts[message["status"]] = counts.get(message["status"], 0) + 1
    later = sum(1 for message in items if message["status"] == "queued" and deferred(message))
    say(f"  {paint('sent', '32')}        {counts.get('sent', 0)}  now, at night: incidents only")
    say(f"  {paint('queued', '33')}      {later}  for 08:00 in {zone}")
    say(f"  {paint('suppressed', '31')}  {counts.get('suppressed', 0)}  over triage-bot's budget")
    say(f"  out of {len(items)} messages from 3 agents", 3)


def main() -> None:
    now = datetime.now(UTC)
    zone = night_zone(now)
    say(paint("Cue: three agents, one on-call engineer", "1"))
    say(f"  dana is in {zone}, where it is {now.astimezone(ZoneInfo(zone)):%H:%M} (quiet hours)")
    say("  starting Cue: SQLite, embedded worker, console connector", 0.2)
    with running_cue() as cue:
        keys = configure(cue, zone)
        triage(cue, keys["triage-bot"])
        reviews(cue, keys["review-bot"], zone)
        billing(cue, keys["billing-bot"], zone)
        summary(cue, zone)


if __name__ == "__main__":
    main()
