"""Append-only, hash-chained evidence ledger and independent verification.

Every row stores the SHA-256 hash of the previous row; recomputing the whole
chain proves (a) order, (b) contents of every record. Key records additionally
embed a snapshot of the object they describe (quiz questions + answers,
grading detail, signature text) so the *object tables* can be checked against
the ledger: silently editing an employee's score afterwards breaks
verification.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Attempt,
    Confirmation,
    EvidenceEvent,
    QuestionType,
    Quiz,
    User,
)

# ---- event catalogue -------------------------------------------------------
EV_USER_CREATED = "user.created"
EV_USER_UPDATED = "user.updated"
EV_QUIZ_CREATED = "quiz.created"
EV_QUESTION_UPDATED = "quiz.questions_updated"
EV_QUIZ_PUBLISHED = "quiz.published"
EV_QUIZ_CLOSED = "quiz.closed"
EV_ASSIGNMENT_OPENED = "assignment.opened"
EV_ASSIGNMENT_CLOSED = "assignment.closed"
EV_LEARNING_VIEWED = "learning.viewed"
EV_ATTEMPT_STARTED = "attempt.started"
EV_ATTEMPT_SUBMITTED = "attempt.submitted"
EV_CONFIRMATION_SIGNED = "confirmation.signed"


def canonical(value) -> str:
    """Deterministic JSON serialization (sorted keys, no whitespace)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


GENESIS_HASH = "0" * 64


def _compute_hash(
    seq: int,
    event_type: str,
    actor_id: int | None,
    refs: dict,
    payload: dict,
    created_at_iso: str,
    prev_hash: str,
) -> str:
    material = "|".join(
        [
            str(seq),
            event_type,
            str(actor_id if actor_id is not None else ""),
            canonical(refs),
            canonical(payload),
            created_at_iso,
            prev_hash,
        ]
    )
    return sha256_text(material)


def record_event(
    db: Session,
    event_type: str,
    payload: dict,
    *,
    actor: User | None = None,
    quiz_id: int | None = None,
    assignment_id: int | None = None,
    attempt_id: int | None = None,
    confirmation_id: int | None = None,
    client_ip: str = "",
    client_ua: str = "",
) -> EvidenceEvent:
    """Append one row under a row lock (SQLite: serialised writes)."""
    last = db.execute(
        select(EvidenceEvent).order_by(EvidenceEvent.seq.desc()).limit(1)
    ).scalar_one_or_none()
    seq = 1 if last is None else last.seq + 1
    prev_hash = GENESIS_HASH if last is None else last.event_hash
    created_at = datetime.utcnow()
    created_at_iso = created_at.isoformat(timespec="seconds")
    refs = {
        "quiz_id": quiz_id,
        "assignment_id": assignment_id,
        "attempt_id": attempt_id,
        "confirmation_id": confirmation_id,
    }
    event_hash = _compute_hash(
        seq,
        event_type,
        actor.id if actor else None,
        refs,
        payload,
        created_at_iso,
        prev_hash,
    )
    event = EvidenceEvent(
        seq=seq,
        event_type=event_type,
        actor_id=actor.id if actor else None,
        actor_employee_no=actor.employee_no if actor else "",
        actor_name=actor.full_name if actor else "",
        quiz_id=quiz_id,
        assignment_id=assignment_id,
        attempt_id=attempt_id,
        confirmation_id=confirmation_id,
        payload=payload,
        prev_hash=prev_hash,
        event_hash=event_hash,
        client_ip=client_ip[:64],
        client_ua=client_ua[:300],
        created_at=created_at,
    )
    db.add(event)
    db.flush()
    return event


# ---- snapshot helpers ------------------------------------------------------
def quiz_snapshot(quiz: Quiz) -> dict:
    return {
        "quiz_id": quiz.id,
        "title": quiz.title,
        "category": quiz.category,
        "version": quiz.version,
        "status": quiz.status.value,
        "pass_scores": {
            "normal": quiz.pass_score_normal,
            "key": quiz.pass_score_key,
            "high": quiz.pass_score_high,
        },
        "questions": [
            {
                "id": q.id,
                "order_index": q.order_index,
                "qtype": q.qtype.value,
                "stem": q.stem,
                "options": (["错误", "正确"] if q.qtype.value == "bool" else q.options),
                "answer": q.answer,
                "score": q.score,
            }
            for q in sorted(quiz.questions, key=lambda x: x.order_index)
        ],
    }


def question_digest(snapshot: dict) -> str:
    return sha256_text(
        canonical([{k: q[k] for k in ("stem", "options", "answer", "score", "qtype")}
                   for q in snapshot["questions"]])
    )


def grade(snapshot_questions: list[dict], answers: dict) -> dict:
    """Independent grading used both at submit time and during verification."""
    details = []
    total = sum(int(q["score"]) for q in snapshot_questions)
    earned = 0
    for q in snapshot_questions:
        qid = str(q["id"])
        raw = answers.get(qid, [])
        if isinstance(raw, int):
            raw = [raw]
        given = sorted(int(x) for x in raw)
        correct = sorted(int(x) for x in q["answer"])
        if q["qtype"] == QuestionType.multiple.value:
            ok = given == correct          # 多选：完全一致才得分
        else:
            ok = given == correct
        if ok:
            earned += int(q["score"])
        details.append(
            {"question_id": q["id"], "given": given, "correct": correct, "ok": ok,
             "score": int(q["score"]) if ok else 0}
        )
    return {"earned": earned, "total": total, "details": details}


def signature_hash(
    employee_no: str, full_name: str, statement: str, signature_text: str,
    score: int, required: int, signed_iso: str,
) -> str:
    return sha256_text(
        "|".join(
            [employee_no, full_name, statement, signature_text,
             str(score), str(required), signed_iso]
        )
    )


# ---- verification ----------------------------------------------------------
@dataclass
class ChainReport:
    ok: bool = True
    rows_checked: int = 0
    errors: list[str] = field(default_factory=list)
    earliest: datetime | None = None
    latest: datetime | None = None


def verify_chain(db: Session, *, limit_event: EvidenceEvent | None = None) -> ChainReport:
    """Recompute every hash and cross-check snapshots against current tables.

    If ``limit_event`` is given (verify a single evidence bundle), the chain is
    verified up to and including that event; the object cross-checks still run
    for every event in range.
    """
    report = ChainReport()
    stmt = select(EvidenceEvent).order_by(EvidenceEvent.seq.asc())
    if limit_event is not None:
        stmt = stmt.where(EvidenceEvent.seq <= limit_event.seq)
    events = list(db.execute(stmt).scalars())

    prev_hash = GENESIS_HASH
    for idx, ev in enumerate(events, start=1):
        report.rows_checked += 1
        if ev.seq != idx:
            report.ok = False
            report.errors.append(f"序号断裂：期望 {idx}，实际 {ev.seq}")
        expected = _compute_hash(
            ev.seq, ev.event_type, ev.actor_id,
            {
                "quiz_id": ev.quiz_id,
                "assignment_id": ev.assignment_id,
                "attempt_id": ev.attempt_id,
                "confirmation_id": ev.confirmation_id,
            },
            ev.payload,
            ev.created_at.isoformat(timespec="seconds"),
            prev_hash,
        )
        if expected != ev.event_hash:
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}（{ev.event_type}）哈希不匹配，记录可能被篡改")
        if ev.prev_hash != prev_hash:
            report.ok = False
            report.errors.append(f"事件 #{ev.seq} 前序哈希断裂")
        prev_hash = ev.event_hash

        try:
            _cross_check(db, ev, report)
        except Exception as exc:  # pragma: no cover - defensive
            report.ok = False
            report.errors.append(f"事件 #{ev.seq} 交叉校验异常：{exc}")

    if events:
        report.earliest = events[0].created_at
        report.latest = events[-1].created_at
    return report


def _cross_check(db: Session, ev: EvidenceEvent, report: ChainReport) -> None:
    p = ev.payload or {}

    if ev.event_type == EV_QUIZ_PUBLISHED:
        quiz = db.get(Quiz, ev.quiz_id) if ev.quiz_id else None
        snap = p.get("snapshot")
        if quiz is None:
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：测验已被删除，无法核对发布快照")
            return
        if quiz.version != snap.get("version"):
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：测验版本号与发布快照不一致")
        if quiz.question_digest and quiz.status.value == "published":
            if quiz.question_digest != p.get("question_digest"):
                report.ok = False
                report.errors.append(f"事件 #{ev.seq}：发布后题库内容被改动（摘要不一致）")

    elif ev.event_type == EV_ATTEMPT_SUBMITTED:
        attempt = db.get(Attempt, ev.attempt_id) if ev.attempt_id else None
        if attempt is None:
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：答卷记录缺失")
            return
        snap = p.get("snapshot", {})
        if snap.get("quiz_id") != attempt.quiz_id or snap.get("version") != attempt.quiz_version:
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：答卷测验/版本与快照不一致")
        # 1) 题目快照必须与作答时题目一致（防改题）
        if snap.get("questions") != attempt.question_snapshot:
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：答卷题目快照被改动")
        # 2) 服务端独立重新判分（防改分）
        re_grade = grade(snap.get("questions", []), attempt.answers or {})
        if re_grade["earned"] != attempt.score or re_grade["total"] != attempt.total_score:
            report.ok = False
            report.errors.append(
                f"事件 #{ev.seq}：成绩被篡改（证据 {re_grade['earned']}/{re_grade['total']}"
                f" vs 当前 {attempt.score}/{attempt.total_score}）"
            )
        passed_now = attempt.score >= attempt.required_pass_score
        if bool(p.get("passed")) != passed_now:
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：通过结论与分数/门槛矛盾")
        if bool(attempt.passed) != passed_now:
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：attempt.passed 标志被改动")
        # 3) 参与者身份
        if p.get("user", {}).get("id") != attempt.user_id:
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：答卷人身份不匹配")

    elif ev.event_type == EV_CONFIRMATION_SIGNED:
        conf = db.get(Confirmation, ev.confirmation_id) if ev.confirmation_id else None
        if conf is None:
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：确认书记录缺失")
            return
        signed_iso = conf.signed_at.isoformat(timespec="seconds")
        recomputed = signature_hash(
            conf.employee_no, conf.full_name, conf.statement, conf.signature_text,
            conf.score, conf.required_pass_score, signed_iso,
        )
        if recomputed != conf.signature_hash or recomputed != p.get("signature_hash"):
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：签署内容或签名被改动")
        if conf.attempt_id != ev.attempt_id or conf.user_id != p.get("user", {}).get("id"):
            report.ok = False
            report.errors.append(f"事件 #{ev.seq}：确认书关联关系被改动")
