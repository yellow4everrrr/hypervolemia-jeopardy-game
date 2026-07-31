"""ASGI entrypoint: ``uvicorn app.main:app``."""

from __future__ import annotations

from app.interfaces.http.app import create_app

app = create_app()
