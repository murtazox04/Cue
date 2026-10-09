"""ASGI entry point for servers: ``uvicorn cue_notify.asgi:app``."""

from cue_notify.api.app import create_app

app = create_app()
