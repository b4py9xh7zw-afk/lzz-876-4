from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common import base_context, templates
from app.database import get_db
from app.evidence import verify_chain
from app.models import EvidenceEvent

router = APIRouter()


@router.get("/verify")
def verify_page(request: Request, code: str | None = None, db: Session = Depends(get_db)):
    event = None
    report = None
    if code:
        event = db.execute(
            select(EvidenceEvent).where(EvidenceEvent.event_hash == code.strip())
        ).scalar_one_or_none()
        if event:
            report = verify_chain(db, limit_event=event)
    return templates.TemplateResponse(request, "verify.html", base_context(request, code=code, event=event, report=report))


@router.post("/verify")
def verify_submit(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    return verify_page(request, code=code, db=db)


@router.get("/health")
def health():
    return {"status": "ok"}
