"""Shared helpers: templates, flash messages, client info, context."""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def dt_display(value) -> str:
    if not value:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def dt_date(value) -> str:
    if not value:
        return "—"
    return value.strftime("%Y-%m-%d")


import json as _json

templates.env.filters["dt"] = dt_display
templates.env.filters["dtonly"] = dt_date
templates.env.filters["json_pretty"] = lambda v: _json.dumps(
    v, ensure_ascii=False, indent=2)


def client_info(request: Request) -> tuple[str, str]:
    ip = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        ip = xff.split(",")[0].strip()
    return ip, request.headers.get("user-agent", "")[:300]


def flash(request: Request, message: str, category: str = "info") -> None:
    msgs = request.session if hasattr(request, "session") else None
    # sessions live in signed cookie middleware; use request.state fallback
    bucket = getattr(request.state, "_flash", None)
    if bucket is None:
        bucket = []
        request.state._flash = bucket
    bucket.append({"message": message, "category": category})


def pop_flash(request: Request) -> list[dict]:
    msgs = getattr(request.state, "_flash", None) or []
    # messages set in a prior redirect hop come via cookie
    cookie_msgs = getattr(request.state, "_cookie_flash", None) or []
    request.state._flash = []
    return cookie_msgs + msgs


def base_context(request: Request, user=None, **extra) -> dict:
    ctx = {
        "request": request,
        "current_user": user,
        "flash_messages": pop_flash(request),
    }
    ctx.update(extra)
    return ctx
