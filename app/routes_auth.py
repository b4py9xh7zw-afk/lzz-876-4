from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common import base_context, client_info, templates
from app.database import get_db
from app.models import User
from app.security import (
    SESSION_COOKIE,
    create_session_token,
    hash_password,
    require_login,
    verify_password,
)

router = APIRouter()


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "session", {}).get("uid"):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "login.html", base_context(request))


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.username == username.strip())).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "login.html", base_context(request, error="工号/用户名或密码错误"),
            status_code=401,
        )
    request.state.session = {"uid": user.id}
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE, create_session_token(user.id),
        httponly=True, samesite="lax", max_age=8 * 3600,
    )
    return resp


@router.post("/logout")
def logout(request: Request):
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/change-password")
def change_password_page(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(request, "change_password.html", base_context(request, user))


@router.post("/change-password")
def change_password_submit(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_login)):
    if not verify_password(old_password, user.password_hash):
        return templates.TemplateResponse(
            request, "change_password.html", base_context(request, user, error="原密码错误"), status_code=400)
    if new_password != confirm or len(new_password) < 8:
        return templates.TemplateResponse(
            request, "change_password.html", base_context(request, user,
            error="两次输入不一致或新密码长度少于 8 位"), status_code=400)
    user.password_hash = hash_password(new_password)
    db.commit()
    return templates.TemplateResponse(
        request, "change_password.html", base_context(request, user, message="密码已更新"))
