from datetime import datetime, timedelta

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


def _assignment(db, category):
    quiz = db.query(Quiz).filter(Quiz.category == category).one()
    return db.query(Assignment).filter(Assignment.quiz_id == quiz.id).one(), quiz


def _open_and_start(c, db, username, assignment):
    c.post("/login", data={"username": username, "password": "compliance123"})
    c.get(f"/assignments/{assignment.id}/learn")
    from tests.test_full_flow import _backdate_learning
    u = db.query(User).filter(User.username == username).one()
    _backdate_learning(db, assignment.id, u)
    r = c.post(f"/assignments/{assignment.id}/start", follow_redirects=False)
    assert r.status_code == 303, r.text[:200]
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _payload(snapshot, force_wrong):
    data = {}
    for q in snapshot:
        key = f"q{q['id']}"
        ans = list(q["answer"])
        if force_wrong:
            alt = [i for i in range(len(q["options"])) if i not in ans] or [0]
            ans = [alt[0]]
        if q["qtype"] == "multiple":
            data[key] = [str(a) for a in ans]
        else:
            data[key] = str(ans[0])
    return data


def test_high_risk_employee_90_gate_and_sign(client, db):
    c = TestClient(client.app)
    assignment, quiz = _assignment(db, "data_security")
    zhang = db.query(User).filter(User.username == "zhang").one()
    assert zhang.risk_tier == RiskTier.high

    attempt_id = _open_and_start(c, db, "zhang", assignment)
    attempt = db.get(Attempt, attempt_id)
    assert attempt.required_pass_score == 90

    # 故意答错一部分 -> 60 分（每题分值组合：20+20+0+20+0+20=80）< 90
    # 先全错一次
    r = c.post(f"/attempts/{attempt_id}/submit",
               data=_payload(attempt.question_snapshot, force_wrong=True),
               follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    attempt = db.get(Attempt, attempt_id)
    assert attempt.passed is False

    # 未达标禁止签署
    r = c.get(f"/attempts/{attempt_id}/sign")
    assert r.status_code == 400 and "及格线" in r.text
    # 结果页提示高风险硬门槛与重考
    r = c.get(f"/attempts/{attempt_id}/result")
    assert "高风险岗位必须达到 90" in r.text

    # 第二次答对 -> 100
    attempt2_id = _open_and_start(c, db, "zhang", assignment)
    attempt2 = db.get(Attempt, attempt2_id)
    c.post(f"/attempts/{attempt2_id}/submit",
           data=_payload(attempt2.question_snapshot, force_wrong=False),
           follow_redirects=False)
    db.expire_all()
    attempt2 = db.get(Attempt, attempt2_id)
    assert attempt2.score == 100 and attempt2.passed is True

    c.post(f"/attempts/{attempt2_id}/sign", data={
        "signature_text": "张敏", "employee_no": "E101", "confirm_identity": "on",
    }, follow_redirects=False)
    db.expire_all()
    assert db.query(Confirmation).filter(
        Confirmation.attempt_id == attempt2_id).count() == 1

    # 花名册逐人反映：通过 + 已签署 + 时间
    c2 = TestClient(client.app)
    c2.post("/login", data={"username": "legal", "password": "compliance123"})
    r = c2.get(f"/publisher/assignments/{assignment.id}/roster")
    assert "E101" in r.text and "张敏" in r.text and "高风险" in r.text
    rcsv = c2.get(f"/publisher/assignments/{assignment.id}/roster.csv")
    assert "E101" in rcsv.text and "已完成签署" in rcsv.text


def test_roster_shows_failed_high_risk_not_just_pass_count(client, db):
    """关键诉求：不能只展示通过人数——未达标的高风险人员必须逐人可见。"""
    c = TestClient(client.app)
    assignment, _ = _assignment(db, "data_security")
    c.post("/login", data={"username": "audit", "password": "compliance123"})
    r = c.get(f"/publisher/assignments/{assignment.id}/roster")
    # wang（高风险，数据中心）在受众内，即使从未作答也要出现
    assert "王强" in r.text and "E102" in r.text
    assert "未开始" in r.text


def test_score_tampering_is_detected(client, db):
    """直接改库把 100 分改成 60（或反向），证据链校验必须报警。"""
    report_before = verify_chain(db)
    assert report_before.ok, report_before.errors

    zhang = db.query(User).filter(User.username == "zhang").one()
    attempt = (db.query(Attempt).join(Confirmation, Confirmation.attempt_id == Attempt.id)
               .filter(Attempt.user_id == zhang.id, Attempt.score == 100).first())
    assert attempt is not None, "高风险签署流程应先生成满分答卷"
    attempt.score = 60
    attempt.passed = False
    db.commit()

    report = verify_chain(db)
    assert not report.ok
    assert any("成绩被篡改" in e for e in report.errors), report.errors

    # 公开核验页同样失败
    ev = db.query(EvidenceEvent).filter(
        EvidenceEvent.attempt_id == attempt.id,
        EvidenceEvent.event_type == "attempt.submitted").one()
    c = TestClient(client.app)
    r = c.get(f"/verify?code={ev.event_hash}", follow_redirects=True)
    assert "核验失败" in r.text and "成绩被篡改" in r.text

    # 还原
    attempt.score = 100
    attempt.passed = True
    db.commit()
    assert verify_chain(db).ok


def test_signature_tampering_is_detected(client, db):
    conf = db.query(Confirmation).filter(Confirmation.employee_no == "E101").first()
    assert conf is not None
    conf.signature_text = "伪造签名"
    db.commit()
    report = verify_chain(db)
    assert not report.ok
    assert any("签名被改动" in e for e in report.errors), report.errors
    conf.signature_text = "张敏"
    db.commit()
    assert verify_chain(db).ok


def test_question_change_after_publish_detected(client, db):
    quiz = db.query(Quiz).filter(Quiz.category == "data_security").one()
    digest_before = quiz.question_digest
    quiz.question_digest = "x" * 64
    db.commit()
    report = verify_chain(db)
    assert not report.ok
    assert any("题库内容被改动" in e for e in report.errors), report.errors
    quiz.question_digest = digest_before
    db.commit()
    assert verify_chain(db).ok


def test_chain_break_detected(client, db):
    ev = db.query(EvidenceEvent).order_by(EvidenceEvent.seq.desc()).first()
    saved = ev.prev_hash
    ev.prev_hash = "f" * 64
    db.commit()
    report = verify_chain(db)
    assert not report.ok and any("前序哈希断裂" in e for e in report.errors)
    ev.prev_hash = saved
    db.commit()
    assert verify_chain(db).ok


def test_staff_can_export_any_employee_evidence_json(client, db):
    zhang = db.query(User).filter(User.username == "zhang").one()
    attempt = (db.query(Attempt).join(Confirmation, Confirmation.attempt_id == Attempt.id)
               .filter(Attempt.user_id == zhang.id).first())
    c = TestClient(client.app)
    c.post("/login", data={"username": "legal", "password": "compliance123"})
    r = c.get(f"/attempts/{attempt.id}/evidence.json")
    assert r.status_code == 200
    assert r.json()["employee"]["employee_no"] == "E101"


def test_employee_cannot_access_others_attempt(client, db):
    chen = db.query(User).filter(User.username == "chen").one()
    zhang = db.query(User).filter(User.username == "zhang").one()
    zhang_att = db.query(Attempt).filter(Attempt.user_id == zhang.id).first()
    c = TestClient(client.app)
    c.post("/login", data={"username": "chen", "password": "compliance123"})
    r = c.get(f"/attempts/{zhang_att.id}/evidence")
    assert r.status_code == 404
