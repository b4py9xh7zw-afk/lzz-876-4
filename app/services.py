"""Domain services: audience matching, pass thresholds, completion status, roster."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Assignment,
    Attempt,
    AttemptStatus,
    Confirmation,
    RiskTier,
    User,
)

RISK_ORDER = {RiskTier.normal: 0, RiskTier.key: 1, RiskTier.high: 2}
RISK_LABELS = {"normal": "普通岗位", "key": "关键岗位", "high": "高风险岗位"}
CATEGORY_LABELS = {
    "anti_fraud": "反舞弊",
    "data_security": "数据安全",
    "procurement": "采购红线",
    "other": "其他合规",
}

MIN_LEARNING_SECONDS = 30  # 打开学习页后至少停留时间才允许开始测验


def pass_score_for(quiz, tier: RiskTier) -> int:
    if tier == RiskTier.high:
        return quiz.pass_score_high
    if tier == RiskTier.key:
        return quiz.pass_score_key
    return quiz.pass_score_normal


def matches_assignment(user: User, assignment: Assignment) -> bool:
    scope = assignment.audience_scope.value
    if scope == "all":
        return True
    if scope == "individual":
        ids = {x.strip() for x in assignment.audience_value.split(",") if x.strip()}
        return str(user.id) in ids
    if scope == "department":
        depts = {x.strip() for x in assignment.audience_value.split(",") if x.strip()}
        return user.department in depts
    if scope == "risk":
        minimum = RiskTier(assignment.audience_value)
        return RISK_ORDER[user.risk_tier] >= RISK_ORDER[minimum]
    return False


def assignments_for_user(db: Session, user: User) -> list[tuple[Assignment, bool]]:
    """(assignment, matched) for assignments of quizzes the user can see."""
    rows = db.execute(
        select(Assignment).order_by(Assignment.opened_at.desc())
    ).scalars().all()
    out = []
    for a in rows:
        if matches_assignment(user, a):
            out.append((a, True))
    return out


def user_assignment_state(db: Session, user: User, assignment: Assignment) -> dict:
    """Aggregates learning / best attempt / signature for one person+task."""
    attempts = list(
        db.execute(
            select(Attempt)
            .where(Attempt.user_id == user.id, Attempt.assignment_id == assignment.id)
            .order_by(Attempt.started_at.asc())
        ).scalars()
    )
    submitted = [a for a in attempts if a.status == AttemptStatus.submitted]
    best = max(submitted, key=lambda a: a.score or -1, default=None)
    confirmation = None
    if best is not None:
        confirmation = db.execute(
            select(Confirmation).where(Confirmation.attempt_id == best.id)
        ).scalar_one_or_none()

    required = pass_score_for(assignment.quiz, user.risk_tier)
    state = "not_started"
    if best is not None and best.score >= required and confirmation is not None:
        state = "completed"
    elif best is not None and best.score >= required:
        state = "passed_pending_signature"
    elif submitted:
        state = "failed"
    elif attempts:
        state = "in_progress"

    return {
        "state": state,
        "attempts": attempts,
        "attempt_count": len(submitted),
        "best": best,
        "required_score": required,
        "confirmation": confirmation,
        "high_risk_hard_gate": user.risk_tier == RiskTier.high and best is not None
        and best.score < required,
    }


def build_roster(db: Session, assignment: Assignment) -> list[dict]:
    """One row per targeted employee — proves per-person status, not just totals."""
    users = list(db.execute(select(User).where(User.is_active.is_(True))).scalars())
    roster = []
    for user in sorted(users, key=lambda u: (u.department, u.employee_no)):
        if not matches_assignment(user, assignment):
            continue
        info = user_assignment_state(db, user, assignment)
        best = info["best"]
        conf = info["confirmation"]
        roster.append(
            {
                "employee_no": user.employee_no,
                "full_name": user.full_name,
                "department": user.department,
                "position": user.position,
                "risk_tier": user.risk_tier.value,
                "risk_label": RISK_LABELS[user.risk_tier.value],
                "required_score": info["required_score"],
                "state": info["state"],
                "attempt_count": info["attempt_count"],
                "best_score": best.score if best else None,
                "passed": (best.score >= info["required_score"]) if best else False,
                "signed": conf is not None,
                "submitted_at": best.submitted_at if best else None,
                "signed_at": conf.signed_at if conf else None,
                "attempt_id": best.id if best else None,
            }
        )
    return roster


def learning_seconds(view_events: list) -> int:
    """Cumulative seconds from first learning.viewed to now (best effort)."""
    if not view_events:
        return 0
    first = min(ev.created_at for ev in view_events)
    return int((datetime.utcnow() - first).total_seconds())
