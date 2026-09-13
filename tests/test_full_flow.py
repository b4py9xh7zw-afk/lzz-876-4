import time

from fastapi.testclient import TestClient

from app.evidence import verify_chain
from app.models import (
    Assignment,
    Attempt,
    Confirmation,
    EvidenceEvent,
    Quiz,
    RiskTier,
    User,
)
from app.seed import ANTI_FRAUD, DATA_SECURITY


def _anti_fraud_assignment(db):
    quiz = db.query(Quiz).filter(Quiz.category == "anti_fraud").one()
    return db.query(Assignment).filter(Assignment.quiz_id == quiz.id).one(), quiz


def _data_assignment(db):
    quiz = db.query(Quiz).filter(Quiz.category == "data_security").one()
    return db.query(Assignment).filter(Assignment.quiz_id == quiz.id).one(), quiz


def test_seed_chain_intact(db):
    report = verify_chain(db)
    assert report.ok, report.errors
    assert report.rows_checked >= 14


def test_login_and_employee_dashboard(client, db):
    c = TestClient(client.app)
    r = c.post("/login", data={"username": "chen", "password": "compliance123"},
               follow_redirects=True)
    assert r.status_code == 200
    assert "反舞弊" in r.text
    # chen 是普通岗 + 市场部，只在全员任务里，不在关键岗任务里
    assert "数据安全红线" not in r.text


def _correct_answers(snapshot):
    out = {}
    for q in snapshot:
        key = f"q{q['id']}"
        for a in q["answer"]:
            out[(key, a)] = q["qtype"]
    return out


def _answer_payload(snapshot, force_wrong=False):
    # httpx multi-value form: list of (key, value) tuples must be wrapped in a dict
    data = {}
    for q in snapshot:
        key = f"q{q['id']}"
        ans = list(q["answer"])
        if force_wrong:
            wrong = [i for i in range(len(q["options"])) if i not in ans] or [0]
            ans = [wrong[0]]
        if q["qtype"] == "multiple":
            data[key] = [str(a) for a in ans]
        else:
            data[key] = str(ans[0])
    return data


def _backdate_learning(db, assignment_id, user=None):
    from datetime import datetime, timedelta
    q = db.query(EvidenceEvent).filter(
        EvidenceEvent.assignment_id == assignment_id,
        EvidenceEvent.event_type == "learning.viewed")
    if user is not None:
        q = q.filter(EvidenceEvent.actor_id == user.id)
    ev = q.order_by(EvidenceEvent.seq.asc()).first()
    ev.created_at = datetime.utcnow() - timedelta(seconds=60)
    # 同步重算该事件哈希，保持证据链有效（生产中不会发生时间改写）
    from app import evidence as evmod
    evs = db.query(EvidenceEvent).order_by(EvidenceEvent.seq.asc()).all()
    prev = evmod.GENESIS_HASH
    for e in evs:
        e.prev_hash = prev
        e.event_hash = evmod._compute_hash(
            e.seq, e.event_type, e.actor_id,
            {"quiz_id": e.quiz_id, "assignment_id": e.assignment_id,
             "attempt_id": e.attempt_id, "confirmation_id": e.confirmation_id},
            e.payload, e.created_at.isoformat(timespec="seconds"), prev)
        prev = e.event_hash
    db.commit()


def test_employee_learning_gate_blocks_early_start(client, db):
    c = TestClient(client.app)
    c.post("/login", data={"username": "chen", "password": "compliance123"})
    assignment, quiz = _anti_fraud_assignment(db)
    c.get(f"/assignments/{assignment.id}/learn")
    r = c.post(f"/assignments/{assignment.id}/start", follow_redirects=False)
    assert r.status_code == 400
    assert "学习时间不足" in r.text


def test_normal_employee_full_flow_with_signature(client, db):
    c = TestClient(client.app)
    c.post("/login", data={"username": "chen", "password": "compliance123"})
    assignment, quiz = _anti_fraud_assignment(db)
    c.get(f"/assignments/{assignment.id}/learn")
    chen = db.query(User).filter(User.username == "chen").one()
    _backdate_learning(db, assignment.id, chen)

    r = c.post(f"/assignments/{assignment.id}/start", follow_redirects=False)
    assert r.status_code == 303
    attempt_id = int(r.headers["location"].rsplit("/", 1)[-1])
    attempt = db.get(Attempt, attempt_id)
    assert attempt.required_pass_score == 60
    assert attempt.question_snapshot[0]["stem"]

    r = c.get(f"/attempts/{attempt_id}")
    assert "交卷" in r.text

    payload = _answer_payload(attempt.question_snapshot)
    r = c.post(f"/attempts/{attempt_id}/submit", data=payload, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    attempt = db.get(Attempt, attempt_id)
    assert attempt.score == 100
    assert attempt.passed is True

    # 不能在达标前签 —— 已达标，直接签
    r = c.get(f"/attempts/{attempt_id}/sign")
    assert "学习确认" in r.text
    r = c.post(f"/attempts/{attempt_id}/sign", data={
        "signature_text": "陈晨", "employee_no": "E103", "confirm_identity": "on",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    db.expire_all()
    conf = db.query(Confirmation).filter(Confirmation.attempt_id == attempt_id).one()
    assert conf.full_name == "陈晨"

    # 证据页包含签名与时间线
    r = c.get(f"/attempts/{attempt_id}/evidence")
    assert "陈晨" in r.text and "哈希链证据时间线" in r.text
    rj = c.get(f"/attempts/{attempt_id}/evidence.json")
    bundle = rj.json()
    assert bundle["confirmation"]["signature_text"] == "陈晨"
    assert len(bundle["evidence_events"]) >= 4

    # 证据哈希可公开核验
    submit_ev = db.query(EvidenceEvent).filter(
        EvidenceEvent.attempt_id == attempt_id,
        EvidenceEvent.event_type == "attempt.submitted").one()
    r = c.get(f"/verify?code={submit_ev.event_hash}", follow_redirects=True)
    assert "核验通过" in r.text


def test_signature_requires_identity_and_correct_employee_no(client, db):
    """zhao(关键岗) 走完全员反舞弊任务并达标，在签署环节校验确认项与工号。"""
    from tests.test_highrisk_and_tamper import _payload
    c = TestClient(client.app)
    assignment, quiz = _anti_fraud_assignment(db)
    c.post("/login", data={"username": "zhao", "password": "compliance123"})
    c.get(f"/assignments/{assignment.id}/learn")
    zhao = db.query(User).filter(User.username == "zhao").one()
    _backdate_learning(db, assignment.id, zhao)
    r = c.post(f"/assignments/{assignment.id}/start", follow_redirects=False)
    att_id = int(r.headers["location"].rsplit("/", 1)[-1])
    att = db.get(Attempt, att_id)
    assert att.required_pass_score == 80
    c.post(f"/attempts/{att_id}/submit", data=_payload(att.question_snapshot, False),
           follow_redirects=False)
    db.expire_all()
    att = db.get(Attempt, att_id)
    assert att.passed is True
    # 未勾选本人确认
    r = c.post(f"/attempts/{att_id}/sign", data={
        "signature_text": "赵磊", "employee_no": "E104",
    })
    assert r.status_code == 400 and "本人确认" in r.text
    # 工号不符
    r = c.post(f"/attempts/{att_id}/sign", data={
        "signature_text": "赵磊", "employee_no": "WRONG", "confirm_identity": "on",
    })
    assert r.status_code == 400 and "工号" in r.text
    # 正确签署成功
    r = c.post(f"/attempts/{att_id}/sign", data={
        "signature_text": "赵磊", "employee_no": "E104", "confirm_identity": "on",
    }, follow_redirects=False)
    assert r.status_code == 303
