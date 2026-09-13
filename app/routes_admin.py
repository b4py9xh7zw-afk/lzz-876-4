import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common import base_context, client_info, templates
from app.database import get_db
from app.evidence import EV_USER_CREATED, EV_USER_UPDATED, record_event
from app.models import RiskTier, Role, User
from app.security import hash_password, require_role
from app.services import RISK_LABELS

router = APIRouter()
admin_only = require_role(Role.admin)


@router.get("/admin/users", dependencies=[Depends(admin_only)])
def user_list(request: Request, db: Session = Depends(get_db), user=Depends(admin_only)):
    users = list(db.execute(select(User).order_by(User.employee_no)).scalars())
    return templates.TemplateResponse(request, "admin_users.html", base_context(request, user, users=users, roles=Role, tiers=RiskTier,
                     risk_labels=RISK_LABELS))


@router.post("/admin/users/create", dependencies=[Depends(admin_only)])
async def user_create(request: Request, db: Session = Depends(get_db), user=Depends(admin_only)):
    form = await request.form()
    employee_no = (form.get("employee_no") or "").strip()
    username = (form.get("username") or "").strip()
    full_name = (form.get("full_name") or "").strip()
    password = form.get("password") or ""
    if not (employee_no and username and full_name and len(password) >= 8):
        raise HTTPException(status_code=400, detail="工号、用户名、姓名必填，密码至少 8 位")
    exists = db.execute(select(User).where(
        (User.employee_no == employee_no) | (User.username == username))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="工号或用户名已存在")
    new_user = User(
        employee_no=employee_no, username=username, full_name=full_name,
        department=(form.get("department") or "").strip(),
        position=(form.get("position") or "").strip(),
        role=Role(form.get("role", "employee")),
        risk_tier=RiskTier(form.get("risk_tier", "normal")),
        password_hash=hash_password(password),
    )
    db.add(new_user)
    db.flush()
    record_event(db, EV_USER_CREATED, {
        "employee_no": new_user.employee_no, "username": new_user.username,
        "full_name": new_user.full_name, "department": new_user.department,
        "position": new_user.position, "role": new_user.role.value,
        "risk_tier": new_user.risk_tier.value,
    }, actor=user)
    db.commit()
    return Response(status_code=303, headers={"Location": "/admin/users"})


@router.post("/admin/users/{user_id}/update", dependencies=[Depends(admin_only)])
async def user_update(request: Request, user_id: int,
                      db: Session = Depends(get_db), actor=Depends(admin_only)):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    before = {"department": target.department, "position": target.position,
              "role": target.role.value, "risk_tier": target.risk_tier.value,
              "is_active": target.is_active}
    target.department = (form.get("department") or "").strip()
    target.position = (form.get("position") or "").strip()
    target.role = Role(form.get("role", target.role.value))
    target.risk_tier = RiskTier(form.get("risk_tier", target.risk_tier.value))
    target.is_active = form.get("is_active") == "on"
    new_pwd = form.get("new_password") or ""
    pwd_changed = False
    if new_pwd:
        if len(new_pwd) < 8:
            raise HTTPException(status_code=400, detail="新密码至少 8 位")
        target.password_hash = hash_password(new_pwd)
        pwd_changed = True
    after = {"department": target.department, "position": target.position,
             "role": target.role.value, "risk_tier": target.risk_tier.value,
             "is_active": target.is_active}
    record_event(db, EV_USER_UPDATED, {
        "target_employee_no": target.employee_no,
        "before": before, "after": after, "password_reset": pwd_changed,
        "changed_at": dt.datetime.utcnow().isoformat(timespec="seconds"),
    }, actor=actor)
    db.commit()
    return Response(status_code=303, headers={"Location": "/admin/users"})
