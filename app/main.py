"""FastAPI application entry point."""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.common import BASE_DIR, templates
from app.database import init_db
from app.routes_admin import router as admin_router
from app.routes_auth import router as auth_router
from app.routes_employee import router as employee_router
from app.routes_public import router as public_router
from app.routes_publisher import router as publisher_router
from app.seed import run_seed
from app.security import SESSION_COOKIE, serializer

app = FastAPI(title="企业内控合规测评平台")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

app.include_router(auth_router)
app.include_router(public_router)
app.include_router(employee_router)
app.include_router(publisher_router)
app.include_router(admin_router)


@app.middleware("http")
async def signed_session_middleware(request: Request, call_next):
    """Minimal signed-cookie session: {"uid":..., "flash":[...]}."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            data = serializer.loads(token, max_age=8 * 3600)
            request.state.session = data
            request.state._cookie_flash = data.get("flash", [])
        except Exception:
            request.state.session = {}
    else:
        request.state.session = {}

    response = await call_next(request)

    session = getattr(request.state, "session", {}) or {}
    pending = getattr(request.state, "_flash", []) or []
    if pending:
        session = {**session, "flash": pending}
    elif session.get("flash"):
        session = {k: v for k, v in session.items() if k != "flash"}
    if session:
        response.set_cookie(
            SESSION_COOKIE, serializer.dumps(session),
            max_age=8 * 3600, httponly=True, samesite="lax",
            secure=os.environ.get("COMPLIANCE_COOKIE_SECURE") == "1",
        )
    return response


@app.get("/")
def index(request: Request):
    session = getattr(request.state, "session", {})
    if session.get("uid"):
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    if os.environ.get("COMPLIANCE_AUTOSEED", "1") == "1":
        run_seed()
