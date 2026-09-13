import datetime as dt
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common import base_context, client_info, templates
from app.database import get_db
from app.evidence import (
    EV_ATTEMPT_STARTED,
    EV_ATTEMPT_SUBMITTED,
    EV_CONFIRMATION_SIGNED,
    EV_LEARNING_VIEWED,
    grade,
    quiz_snapshot,
    record_event,
    signature_hash,
)
from app.models import (
    Assignment,
    Attempt,
    AttemptStatus,
    Confirmation,
    EvidenceEvent,
    Quiz,
    QuizStatus,
    Role,
)
from app.security import require_login
from app.services import (
    CATEGORY_LABELS,
    MIN_LEARNING_SECONDS,
    RISK_LABELS,
    assignments_for_user,
    matches_assignment,
    pass_score_for,
    user_assignment_state,
)

router = APIRouter()

CONFIRMATION_STATEMENT = (
    "本人确认：已认真学习《{title}》的全部学习材料，独立完成测验，"
    "知晓并理解相关合规红线要求；本人承诺在工作中严格遵守上述规定，"
    "如违反相关制度，自愿承担相应纪律直至法律责任。"
)


def _bounded_assignment(db: Session, assignment_id: int, user) -> Assignment:
    a = db.get(Assignment, assignment_id)
    if a is None or not matches_assignment(user, a):
        raise HTTPException(status_code=404, detail="任务不存在或不属于你")
    return a


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    if user.role in (Role.publisher, Role.admin):
        return _staff_dashboard(request, db, user)

    items = []
    for assignment, _ in assignments_for_user(db, user):
        if assignment.closed_at:
            continue
        info = user_assignment_state(db, user, assignment)
        items.append({"assignment": assignment, "info": info, "quiz": assignment.quiz})
    attempts = list(
        db.execute(
            select(Attempt).where(Attempt.user_id == user.id).order_by(Attempt.started_at.desc())
        ).scalars()
    )
    return templates.TemplateResponse(request, "dashboard_employee.html", base_context(request, user, items=items, attempts=attempts,
                     risk_labels=RISK_LABELS, category_labels=CATEGORY_LABELS))


def _staff_dashboard(request: Request, db: Session, user):
    assignments = list(
        db.execute(select(Assignment).order_by(Assignment.opened_at.desc())).scalars()
    )
    quizzes = list(db.execute(select(Quiz).order_by(Quiz.created_at.desc())).scalars())
    recent_events = list(
        db.execute(select(EvidenceEvent).order_by(EvidenceEvent.seq.desc()).limit(12)).scalars()
    )
    return templates.TemplateResponse(request, "dashboard_staff.html", base_context(request, user, assignments=assignments, quizzes=quizzes,
                     recent_events=recent_events, category_labels=CATEGORY_LABELS))


@router.get("/assignments/{assignment_id}/learn")
def learn_page(request: Request, assignment_id: int,
               db: Session = Depends(get_db), user=Depends(require_login)):
    assignment = _bounded_assignment(db, assignment_id, user)
    quiz = assignment.quiz
    ip, ua = client_info(request)
    record_event(db, EV_LEARNING_VIEWED, {
        "learning_content_sha256": hashlib.sha256(
            (quiz.learning_content or "").encode("utf-8")).hexdigest(),
        "viewed_at": dt.datetime.utcnow().isoformat(timespec="seconds"),
    }, actor=user, quiz_id=quiz.id, assignment_id=assignment.id,
       client_ip=ip, client_ua=ua)
    db.commit()
    info = user_assignment_state(db, user, assignment)
    return templates.TemplateResponse(request, "learn.html", base_context(request, user, quiz=quiz, assignment=assignment, info=info,
                     min_learning_seconds=MIN_LEARNING_SECONDS,
                     category_label=CATEGORY_LABELS.get(quiz.category, quiz.category),
                     risk_labels=RISK_LABELS))


@router.post("/assignments/{assignment_id}/start")
async def start_attempt(request: Request, assignment_id: int,
                        db: Session = Depends(get_db), user=Depends(require_login)):
    assignment = _bounded_assignment(db, assignment_id, user)
    quiz = assignment.quiz
    if quiz.status != QuizStatus.published or assignment.closed_at:
        raise HTTPException(status_code=400, detail="测验未发布或任务已关闭")

    await request.form()  # consume body
    viewed = list(db.execute(
        select(EvidenceEvent).where(
            EvidenceEvent.event_type == EV_LEARNING_VIEWED,
            EvidenceEvent.assignment_id == assignment.id,
            EvidenceEvent.actor_id == user.id,
        )
    ).scalars())
    if not viewed:
        raise HTTPException(status_code=400, detail="请先打开学习材料")
    seconds = int((dt.datetime.utcnow() - min(v.created_at for v in viewed)).total_seconds())
    if seconds < MIN_LEARNING_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"学习时间不足，请认真学习材料至少 {MIN_LEARNING_SECONDS} 秒（当前 {seconds} 秒）")

    existing = list(db.execute(select(Attempt).where(
        Attempt.user_id == user.id, Attempt.assignment_id == assignment.id,
        Attempt.status == AttemptStatus.in_progress)).scalars())
    if existing:
        return Response(status_code=303, headers={"Location": f"/attempts/{existing[0].id}"})

    required = pass_score_for(quiz, user.risk_tier)
    snapshot = quiz_snapshot(quiz)
    attempt = Attempt(
        quiz_id=quiz.id, assignment_id=assignment.id, user_id=user.id,
        quiz_version=quiz.version, question_snapshot=snapshot["questions"],
        required_pass_score=required,
        total_score=sum(q["score"] for q in snapshot["questions"]),
        max_attempts=3,
    )
    ip, ua = client_info(request)
    attempt.client_ip, attempt.client_ua = ip, ua
    db.add(attempt)
    db.flush()
    record_event(db, EV_ATTEMPT_STARTED, {
        "quiz_title": quiz.title, "quiz_version": quiz.version,
        "required_pass_score": required,
        "risk_tier": user.risk_tier.value,
        "question_count": len(snapshot["questions"]),
        "total_score": attempt.total_score,
        "started_at": attempt.started_at.isoformat(timespec="seconds"),
    }, actor=user, quiz_id=quiz.id, assignment_id=assignment.id,
       attempt_id=attempt.id, client_ip=ip, client_ua=ua)
    db.commit()
    return Response(status_code=303, headers={"Location": f"/attempts/{attempt.id}"})


@router.get("/attempts/{attempt_id}")
def attempt_page(request: Request, attempt_id: int,
                 db: Session = Depends(get_db), user=Depends(require_login)):
    attempt = db.get(Attempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise HTTPException(status_code=404)
    if attempt.status == AttemptStatus.submitted:
        return Response(status_code=303, headers={"Location": f"/attempts/{attempt_id}/result"})
    assignment = db.get(Assignment, attempt.assignment_id)
    return templates.TemplateResponse(request, "attempt.html", base_context(request, user, attempt=attempt, assignment=assignment,
                     quiz=attempt.quiz, questions=attempt.question_snapshot,
                     required=attempt.required_pass_score))


def _collect_answers(form, questions: list[dict]) -> dict:
    answers: dict[str, list[int]] = {}
    for q in questions:
        key = f"q{q['id']}"
        if q["qtype"] == "multiple":
            values = form.getlist(key)
            answers[str(q["id"])] = sorted(int(v) for v in values)
        else:
            value = form.get(key)
            answers[str(q["id"])] = [int(value)] if value not in (None, "") else []
    return answers


@router.post("/attempts/{attempt_id}/submit")
async def submit_attempt(request: Request, attempt_id: int,
                         db: Session = Depends(get_db), user=Depends(require_login)):
    attempt = db.get(Attempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise HTTPException(status_code=404)
    if attempt.status == AttemptStatus.submitted:
        return Response(status_code=303, headers={"Location": f"/attempts/{attempt_id}/result"})

    form = await request.form()
    answers = _collect_answers(form, attempt.question_snapshot)
    result = grade(attempt.question_snapshot, answers)
    attempt.answers = answers
    attempt.grading = result
    attempt.score = result["earned"]
    attempt.total_score = result["total"]
    attempt.passed = result["earned"] >= attempt.required_pass_score
    attempt.status = AttemptStatus.submitted
    attempt.submitted_at = dt.datetime.utcnow()
    ip, ua = client_info(request)

    # 已提交次数（含本次），超出上限不得再次发起
    submitted_count = len(list(db.execute(select(Attempt).where(
        Attempt.user_id == user.id,
        Attempt.assignment_id == attempt.assignment_id,
        Attempt.status == AttemptStatus.submitted)).scalars())) + 1

    record_event(db, EV_ATTEMPT_SUBMITTED, {
        "submitted_at": attempt.submitted_at.isoformat(timespec="seconds"),
        "user": {"id": user.id, "employee_no": user.employee_no,
                 "full_name": user.full_name, "department": user.department,
                 "position": user.position, "risk_tier": user.risk_tier.value},
        "snapshot": {
            "quiz_id": attempt.quiz_id,
            "version": attempt.quiz_version,
            "questions": attempt.question_snapshot,
        },
        "answers": answers,
        "grading": result,
        "score": attempt.score,
        "total_score": attempt.total_score,
        "required_pass_score": attempt.required_pass_score,
        "passed": attempt.passed,
        "submitted_attempt_count": submitted_count,
        "client_ip": ip,
    }, actor=user, quiz_id=attempt.quiz_id, assignment_id=attempt.assignment_id,
       attempt_id=attempt.id, client_ip=ip, client_ua=ua)
    db.commit()
    return Response(status_code=303, headers={"Location": f"/attempts/{attempt_id}/result"})


@router.get("/attempts/{attempt_id}/result")
def result_page(request: Request, attempt_id: int,
                db: Session = Depends(get_db), user=Depends(require_login)):
    attempt = db.get(Attempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise HTTPException(status_code=404)
    if attempt.status != AttemptStatus.submitted:
        return Response(status_code=303, headers={"Location": f"/attempts/{attempt_id}"})
    assignment = db.get(Assignment, attempt.assignment_id)
    confirmation = db.execute(
        select(Confirmation).where(Confirmation.attempt_id == attempt.id)
    ).scalar_one_or_none()
    submitted_ev = db.execute(select(EvidenceEvent).where(
        EvidenceEvent.attempt_id == attempt.id,
        EvidenceEvent.event_type == EV_ATTEMPT_SUBMITTED)).scalar_one()
    # 同任务历次提交（含进行中数量），控制 3 次机会
    attempts_all = list(db.execute(select(Attempt).where(
        Attempt.user_id == user.id, Attempt.assignment_id == attempt.assignment_id
    ).order_by(Attempt.started_at.asc())).scalars())
    submitted_total = sum(1 for a in attempts_all if a.status == AttemptStatus.submitted)
    can_retry = not attempt.passed and submitted_total < attempt.max_attempts
    return templates.TemplateResponse(request, "result.html", base_context(request, user, attempt=attempt, assignment=assignment,
                     quiz=attempt.quiz, confirmation=confirmation,
                     submit_event=submitted_ev, can_retry=can_retry,
                     submitted_total=submitted_total))


@router.get("/attempts/{attempt_id}/sign")
def sign_page(request: Request, attempt_id: int,
              db: Session = Depends(get_db), user=Depends(require_login)):
    attempt = db.get(Attempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise HTTPException(status_code=404)
    if not attempt.passed:
        raise HTTPException(status_code=400, detail="未达到及格线，不能签署学习确认")
    existing = db.execute(
        select(Confirmation).where(Confirmation.attempt_id == attempt.id)
    ).scalar_one_or_none()
    if existing:
        return Response(status_code=303,
                        headers={"Location": f"/attempts/{attempt_id}/evidence"})
    assignment = db.get(Assignment, attempt.assignment_id)
    statement = CONFIRMATION_STATEMENT.format(title=attempt.quiz.title)
    return templates.TemplateResponse(request, "sign.html", base_context(request, user, attempt=attempt, quiz=attempt.quiz,
                     assignment=assignment, statement=statement))


@router.post("/attempts/{attempt_id}/sign")
async def sign_submit(request: Request, attempt_id: int,
                      db: Session = Depends(get_db), user=Depends(require_login)):
    attempt = db.get(Attempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise HTTPException(status_code=404)
    if not attempt.passed:
        raise HTTPException(status_code=400, detail="未达到及格线，不能签署学习确认")
    existing = db.execute(
        select(Confirmation).where(Confirmation.attempt_id == attempt.id)
    ).scalar_one_or_none()
    if existing:
        return Response(status_code=303,
                        headers={"Location": f"/attempts/{attempt_id}/evidence"})

    form = await request.form()
    signature_text = (form.get("signature_text") or "").strip()
    confirm_identity = form.get("confirm_identity")
    employee_no = (form.get("employee_no") or "").strip()
    if confirm_identity != "on":
        raise HTTPException(status_code=400, detail="必须勾选本人确认")
    if not signature_text:
        raise HTTPException(status_code=400, detail="请输入电子签名")
    if employee_no != user.employee_no:
        raise HTTPException(status_code=400, detail="工号与本人不一致，无法签署")

    statement = CONFIRMATION_STATEMENT.format(title=attempt.quiz.title)
    signed_at = dt.datetime.utcnow()
    signed_iso = signed_at.isoformat(timespec="seconds")
    sig_hash = signature_hash(
        user.employee_no, user.full_name, statement, signature_text,
        attempt.score, attempt.required_pass_score, signed_iso)
    ip, ua = client_info(request)
    conf = Confirmation(
        attempt_id=attempt.id, user_id=user.id, quiz_id=attempt.quiz_id,
        employee_no=user.employee_no, full_name=user.full_name,
        statement=statement, signature_text=signature_text, signature_hash=sig_hash,
        score=attempt.score, required_pass_score=attempt.required_pass_score,
        client_ip=ip, client_ua=ua, signed_at=signed_at,
    )
    db.add(conf)
    db.flush()
    record_event(db, EV_CONFIRMATION_SIGNED, {
        "signed_at": signed_iso,
        "user": {"id": user.id, "employee_no": user.employee_no,
                 "full_name": user.full_name, "department": user.department,
                 "position": user.position, "risk_tier": user.risk_tier.value},
        "statement": statement,
        "signature_text": signature_text,
        "signature_hash": sig_hash,
        "score": attempt.score,
        "required_pass_score": attempt.required_pass_score,
        "passed": attempt.passed,
        "attempt_event_hash": db.execute(select(EvidenceEvent.event_hash).where(
            EvidenceEvent.attempt_id == attempt.id,
            EvidenceEvent.event_type == EV_ATTEMPT_SUBMITTED)).scalar_one(),
        "client_ip": ip,
    }, actor=user, quiz_id=attempt.quiz_id, assignment_id=attempt.assignment_id,
       attempt_id=attempt.id, confirmation_id=conf.id, client_ip=ip, client_ua=ua)
    db.commit()
    return Response(status_code=303,
                    headers={"Location": f"/attempts/{attempt_id}/evidence"})


def _evidence_bundle(db: Session, attempt: Attempt) -> dict:
    events = list(db.execute(select(EvidenceEvent).where(
        EvidenceEvent.assignment_id == attempt.assignment_id,
        EvidenceEvent.actor_id == attempt.user_id,
    ).order_by(EvidenceEvent.seq.asc())).scalars())
    conf = db.execute(select(Confirmation).where(
        Confirmation.attempt_id == attempt.id)).scalar_one_or_none()
    return {
        "platform": "企业内控合规测评平台",
        "exported_at": dt.datetime.utcnow().isoformat(timespec="seconds"),
        "employee": {"employee_no": attempt.user.employee_no,
                     "full_name": attempt.user.full_name,
                     "department": attempt.user.department,
                     "position": attempt.user.position,
                     "risk_tier": attempt.user.risk_tier.value},
        "quiz": {"id": attempt.quiz_id, "title": attempt.quiz.title,
                 "version": attempt.quiz_version},
        "attempt": {"id": attempt.id, "score": attempt.score,
                    "total_score": attempt.total_score,
                    "required_pass_score": attempt.required_pass_score,
                    "passed": attempt.passed,
                    "started_at": attempt.started_at.isoformat(timespec="seconds"),
                    "submitted_at": attempt.submitted_at.isoformat(timespec="seconds")
                    if attempt.submitted_at else None,
                    "client_ip": attempt.client_ip},
        "confirmation": None if conf is None else {
            "id": conf.id, "signature_text": conf.signature_text,
            "signature_hash": conf.signature_hash,
            "statement": conf.statement,
            "signed_at": conf.signed_at.isoformat(timespec="seconds"),
            "client_ip": conf.client_ip,
        },
        "evidence_events": [
            {"seq": e.seq, "event_type": e.event_type,
             "created_at": e.created_at.isoformat(timespec="seconds"),
             "actor": e.actor_name, "actor_employee_no": e.actor_employee_no,
             "payload": e.payload, "prev_hash": e.prev_hash,
             "event_hash": e.event_hash, "client_ip": e.client_ip,
             "client_ua": e.client_ua}
            for e in events
        ],
    }


@router.get("/attempts/{attempt_id}/evidence")
def evidence_page(request: Request, attempt_id: int,
                  db: Session = Depends(get_db), user=Depends(require_login)):
    attempt = db.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404)
    if user.role == Role.employee and attempt.user_id != user.id:
        raise HTTPException(status_code=404)
    events = list(db.execute(select(EvidenceEvent).where(
        EvidenceEvent.attempt_id == attempt.id)).scalars())
    # 学习事件也展示
    learn_events = list(db.execute(select(EvidenceEvent).where(
        EvidenceEvent.assignment_id == attempt.assignment_id,
        EvidenceEvent.actor_id == attempt.user_id,
        EvidenceEvent.event_type == EV_LEARNING_VIEWED)).scalars())
    conf = db.execute(select(Confirmation).where(
        Confirmation.attempt_id == attempt.id)).scalar_one_or_none()
    submit_event = next((e for e in events if e.event_type == EV_ATTEMPT_SUBMITTED), None)
    sign_event = next((e for e in events if e.event_type == EV_CONFIRMATION_SIGNED), None)
    return templates.TemplateResponse(request, "evidence.html", base_context(request, user, attempt=attempt, quiz=attempt.quiz,
                     confirmation=conf, learn_events=learn_events,
                     submit_event=submit_event, sign_event=sign_event))


@router.get("/attempts/{attempt_id}/evidence.json")
def evidence_json(attempt_id: int,
                  db: Session = Depends(get_db), user=Depends(require_login)):
    attempt = db.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404)
    if user.role == Role.employee and attempt.user_id != user.id:
        raise HTTPException(status_code=404)
    from fastapi.responses import JSONResponse
    return JSONResponse(_evidence_bundle(db, attempt))
