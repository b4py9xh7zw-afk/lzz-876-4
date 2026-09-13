import csv
import datetime as dt
import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common import base_context, client_info, templates
from app.database import get_db
from app.evidence import (
    EV_ASSIGNMENT_CLOSED,
    EV_ASSIGNMENT_OPENED,
    EV_QUIZ_CLOSED,
    EV_QUIZ_CREATED,
    EV_QUIZ_PUBLISHED,
    EV_QUESTION_UPDATED,
    question_digest,
    quiz_snapshot,
    record_event,
    verify_chain,
)
from app.models import (
    Assignment,
    AssignmentScope,
    Attempt,
    EvidenceEvent,
    Question,
    QuestionType,
    Quiz,
    QuizStatus,
    RiskTier,
    Role,
    User,
)
from app.security import require_role
from app.services import CATEGORY_LABELS, RISK_LABELS, build_roster

router = APIRouter()
staff = require_role(Role.publisher, Role.admin)

STATE_LABELS = {
    "not_started": "未开始",
    "in_progress": "进行中",
    "failed": "未达标",
    "passed_pending_signature": "达标待签署",
    "completed": "已完成签署",
}

MAX_QUESTIONS = 20


@router.get("/publisher/quizzes", dependencies=[Depends(staff)])
def quiz_list(request: Request, db: Session = Depends(get_db), user=Depends(staff)):
    quizzes = list(db.execute(select(Quiz).order_by(Quiz.created_at.desc())).scalars())
    stats = {}
    for q in quizzes:
        stats[q.id] = db.execute(
            select(func.count(Attempt.id)).where(
                Attempt.quiz_id == q.id)
        ).scalar_one()
    return templates.TemplateResponse(request, "publisher_quizzes.html", base_context(request, user, quizzes=quizzes, stats=stats,
                     category_labels=CATEGORY_LABELS))


@router.post("/publisher/quizzes/create", dependencies=[Depends(staff)])
async def create_quiz(request: Request, db: Session = Depends(get_db), user=Depends(staff)):
    form = await request.form()
    title = (form.get("title") or "").strip()
    category = form.get("category", "other")
    if not title:
        raise HTTPException(status_code=400, detail="标题必填")
    quiz = Quiz(
        title=title, category=category,
        description=(form.get("description") or "").strip(),
        learning_content=(form.get("learning_content") or "").strip(),
        pass_score_normal=int(form.get("pass_score_normal", 60)),
        pass_score_key=int(form.get("pass_score_key", 80)),
        pass_score_high=int(form.get("pass_score_high", 90)),
        time_limit_minutes=int(form.get("time_limit_minutes", 30) or 0) or None,
        status=QuizStatus.draft, created_by_id=user.id,
    )
    db.add(quiz)
    db.flush()
    record_event(db, EV_QUIZ_CREATED, {
        "title": quiz.title, "category": quiz.category, "version": quiz.version,
        "pass_scores": {"normal": quiz.pass_score_normal, "key": quiz.pass_score_key,
                        "high": quiz.pass_score_high},
    }, actor=user, quiz_id=quiz.id)
    db.commit()
    return Response(status_code=303, headers={"Location": f"/publisher/quizzes/{quiz.id}/edit"})


@router.get("/publisher/quizzes/{quiz_id}/edit", dependencies=[Depends(staff)])
def edit_quiz_page(request: Request, quiz_id: int,
                   db: Session = Depends(get_db), user=Depends(staff)):
    quiz = db.get(Quiz, quiz_id)
    if quiz is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "publisher_quiz_edit.html", base_context(request, user, quiz=quiz, qtypes=QuestionType,
                     max_questions=MAX_QUESTIONS, category_labels=CATEGORY_LABELS))


@router.post("/publisher/quizzes/{quiz_id}/edit", dependencies=[Depends(staff)])
async def edit_quiz_save(request: Request, quiz_id: int,
                         db: Session = Depends(get_db), user=Depends(staff)):
    quiz = db.get(Quiz, quiz_id)
    if quiz is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    before = {"title": quiz.title, "description": quiz.description,
              "learning_content": quiz.learning_content}
    quiz.title = (form.get("title") or quiz.title).strip()
    quiz.description = (form.get("description") or "").strip()
    quiz.learning_content = (form.get("learning_content") or "").strip()
    quiz.pass_score_normal = int(form.get("pass_score_normal", quiz.pass_score_normal))
    quiz.pass_score_key = int(form.get("pass_score_key", quiz.pass_score_key))
    quiz.pass_score_high = int(form.get("pass_score_high", quiz.pass_score_high))

    changed_questions = []
    for idx in range(MAX_QUESTIONS):
        stem = (form.get(f"stem_{idx}") or "").strip()
        if not stem:
            continue
        qtype = QuestionType(form.get(f"qtype_{idx}", "single"))
        if qtype == QuestionType.bool:
            options = ["错误", "正确"]
        else:
            options = [
                (form.get(f"opt_{idx}_{i}") or "").strip()
                for i in range(4)
                if (form.get(f"opt_{idx}_{i}") or "").strip()
            ]
            if len(options) < 2:
                raise HTTPException(status_code=400, detail=f"第 {idx + 1} 题至少需要 2 个选项")
        answers = form.getlist(f"ans_{idx}")
        if qtype != QuestionType.multiple and len(answers) > 1:
            answers = answers[:1]
        answer_idxs = sorted(int(a.split(":")[1]) for a in answers if a.startswith(f"{idx}:"))
        if not answer_idxs or any(a >= len(options) for a in answer_idxs):
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 题必须标记正确答案")
        score = int(form.get(f"score_{idx}", 10) or 10)

        qid_raw = form.get(f"qid_{idx}", "").strip()
        if qid_raw:
            q = db.get(Question, int(qid_raw))
            if q and q.quiz_id == quiz.id:
                changed_questions.append({
                    "question_id": q.id,
                    "stem_changed": q.stem != stem or q.answer != answer_idxs,
                })
                q.qtype, q.stem, q.options = qtype, stem, options
                q.answer, q.score, q.order_index = answer_idxs, score, idx
        else:
            q = Question(quiz_id=quiz.id, order_index=idx, qtype=qtype,
                         stem=stem, options=options, answer=answer_idxs, score=score)
            db.add(q)
            changed_questions.append({"question_id": None, "stem": stem[:40]})

    # 删除被清空的题目
    keep_ids = {int(x) for x in form.getlist("qid_present") if x}
    for q in list(quiz.questions):
        if q.id not in keep_ids:
            db.delete(q)

    db.flush()
    after_snap = quiz_snapshot(quiz)
    record_event(db, EV_QUESTION_UPDATED, {
        "meta_before": before,
        "meta_after": {"title": quiz.title, "description": quiz.description,
                       "learning_content": quiz.learning_content},
        "pass_scores": {"normal": quiz.pass_score_normal, "key": quiz.pass_score_key,
                        "high": quiz.pass_score_high},
        "question_count": len(after_snap["questions"]),
        "question_digest_now": question_digest(after_snap),
        "changed": changed_questions,
        "note": "草稿编辑：不影响已开始/已提交的历史答卷（题目在开卷时已快照）",
    }, actor=user, quiz_id=quiz.id)
    db.commit()
    return Response(status_code=303, headers={"Location": f"/publisher/quizzes/{quiz.id}/edit"})


@router.post("/publisher/quizzes/{quiz_id}/publish", dependencies=[Depends(staff)])
async def publish_quiz(request: Request, quiz_id: int,
                       db: Session = Depends(get_db), user=Depends(staff)):
    quiz = db.get(Quiz, quiz_id)
    if quiz is None:
        raise HTTPException(status_code=404)
    if not quiz.questions:
        raise HTTPException(status_code=400, detail="请先至少添加一道题目")
    await request.form()
    if quiz.status == QuizStatus.published:
        raise HTTPException(status_code=400, detail="测验已发布；修改题目会产生新版本")
    quiz.version += 1 if quiz.status == QuizStatus.closed else 0
    snap = quiz_snapshot(quiz)
    quiz.question_digest = question_digest(snap)
    quiz.status = QuizStatus.published
    quiz.published_at = dt.datetime.utcnow()
    record_event(db, EV_QUIZ_PUBLISHED, {
        "snapshot": snap, "question_digest": quiz.question_digest,
        "pass_scores": snap["pass_scores"],
        "published_at": quiz.published_at.isoformat(timespec="seconds"),
    }, actor=user, quiz_id=quiz.id)
    db.commit()
    return Response(status_code=303,
                    headers={"Location": f"/publisher/quizzes/{quiz_id}/assign"})


@router.post("/publisher/quizzes/{quiz_id}/close", dependencies=[Depends(staff)])
async def close_quiz(request: Request, quiz_id: int,
                     db: Session = Depends(get_db), user=Depends(staff)):
    quiz = db.get(Quiz, quiz_id)
    await request.form()
    if quiz and quiz.status == QuizStatus.published:
        quiz.status = QuizStatus.closed
        quiz.closed_at = dt.datetime.utcnow()
        record_event(db, EV_QUIZ_CLOSED, {
            "closed_at": quiz.closed_at.isoformat(timespec="seconds"),
        }, actor=user, quiz_id=quiz.id)
        db.commit()
    return Response(status_code=303, headers={"Location": "/publisher/quizzes"})


@router.get("/publisher/quizzes/{quiz_id}/assign", dependencies=[Depends(staff)])
def assign_page(request: Request, quiz_id: int,
                db: Session = Depends(get_db), user=Depends(staff)):
    quiz = db.get(Quiz, quiz_id)
    if quiz is None or quiz.status != QuizStatus.published:
        raise HTTPException(status_code=404, detail="测验不存在或未发布")
    employees = list(db.execute(
        select(User).where(User.role == Role.employee, User.is_active.is_(True))
        .order_by(User.department, User.employee_no)).scalars())
    departments = sorted({u.department for u in employees if u.department})
    assignments = list(db.execute(
        select(Assignment).where(Assignment.quiz_id == quiz_id)
        .order_by(Assignment.opened_at.desc())).scalars())
    return templates.TemplateResponse(request, "publisher_assign.html", base_context(request, user, quiz=quiz, employees=employees,
                     departments=departments, assignments=assignments,
                     risk_labels=RISK_LABELS, scopes=AssignmentScope))


@router.post("/publisher/quizzes/{quiz_id}/assign", dependencies=[Depends(staff)])
async def open_assignment(request: Request, quiz_id: int,
                          db: Session = Depends(get_db), user=Depends(staff)):
    quiz = db.get(Quiz, quiz_id)
    if quiz is None or quiz.status != QuizStatus.published:
        raise HTTPException(status_code=400, detail="测验未发布")
    form = await request.form()
    scope = AssignmentScope(form.get("scope", "all"))
    value = ""
    if scope == AssignmentScope.department:
        value = ",".join(form.getlist("departments"))
    elif scope == AssignmentScope.risk:
        value = form.get("risk_min", "normal")
        RiskTier(value)
    elif scope == AssignmentScope.individual:
        value = ",".join(form.getlist("user_ids"))
        if not value:
            raise HTTPException(status_code=400, detail="请至少选择一名员工")
    due_raw = (form.get("due_at") or "").strip()
    due = dt.datetime.fromisoformat(due_raw) if due_raw else None
    title = (form.get("title") or quiz.title).strip()

    assignment = Assignment(
        quiz_id=quiz.id, title=title, audience_scope=scope, audience_value=value,
        required=form.get("required", "on") == "on",
        due_at=due, created_by_id=user.id,
    )
    db.add(assignment)
    db.flush()
    record_event(db, EV_ASSIGNMENT_OPENED, {
        "title": title, "scope": scope.value, "audience_value": value,
        "opened_at": assignment.opened_at.isoformat(timespec="seconds"),
        "due_at": due.isoformat(timespec="seconds") if due else None,
        "required": assignment.required,
    }, actor=user, quiz_id=quiz.id, assignment_id=assignment.id)
    db.commit()
    return Response(status_code=303,
                    headers={"Location": f"/publisher/assignments/{assignment.id}/roster"})


@router.post("/publisher/assignments/{assignment_id}/close", dependencies=[Depends(staff)])
async def close_assignment(request: Request, assignment_id: int,
                           db: Session = Depends(get_db), user=Depends(staff)):
    assignment = db.get(Assignment, assignment_id)
    await request.form()
    if assignment and not assignment.closed_at:
        assignment.closed_at = dt.datetime.utcnow()
        record_event(db, EV_ASSIGNMENT_CLOSED, {
            "closed_at": assignment.closed_at.isoformat(timespec="seconds"),
        }, actor=user, quiz_id=assignment.quiz_id, assignment_id=assignment.id)
        db.commit()
    return Response(status_code=303,
                    headers={"Location": f"/publisher/assignments/{assignment_id}/roster"})


@router.get("/publisher/assignments/{assignment_id}/roster", dependencies=[Depends(staff)])
def roster_page(request: Request, assignment_id: int,
                db: Session = Depends(get_db), user=Depends(staff)):
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404)
    roster = build_roster(db, assignment)
    counts = {"total": len(roster), "completed": 0, "passed_pending_signature": 0,
              "failed": 0, "not_started": 0, "in_progress": 0,
              "signed": 0, "passed": 0}
    for row in roster:
        counts[row["state"]] += 1
        if row["signed"]:
            counts["signed"] += 1
        if row["passed"]:
            counts["passed"] += 1
    return templates.TemplateResponse(request, "publisher_roster.html", base_context(request, user, assignment=assignment, roster=roster,
                     counts=counts, state_labels=STATE_LABELS, risk_labels=RISK_LABELS))


@router.get("/publisher/assignments/{assignment_id}/roster.csv", dependencies=[Depends(staff)])
def roster_csv(assignment_id: int,
               db: Session = Depends(get_db), user=Depends(staff)):
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404)
    roster = build_roster(db, assignment)
    buf = io.StringIO()
    buf.write("﻿")  # Excel BOM
    writer = csv.writer(buf)
    writer.writerow(["工号", "姓名", "部门", "岗位", "风险等级", "个人及格线",
                     "状态", "提交次数", "最好成绩", "是否达标", "是否签署",
                     "交卷时间(UTC)", "签署时间(UTC)", "证据链接"])
    for row in roster:
        writer.writerow([
            row["employee_no"], row["full_name"], row["department"], row["position"],
            row["risk_label"], row["required_score"],
            STATE_LABELS.get(row["state"], row["state"]),
            row["attempt_count"],
            row["best_score"] if row["best_score"] is not None else "",
            "是" if row["passed"] else "否",
            "是" if row["signed"] else "否",
            row["submitted_at"].strftime("%Y-%m-%d %H:%M:%S") if row["submitted_at"] else "",
            row["signed_at"].strftime("%Y-%m-%d %H:%M:%S") if row["signed_at"] else "",
            f"/attempts/{row['attempt_id']}/evidence" if row["attempt_id"] else "",
        ])
    data = buf.getvalue().encode("utf-8")
    headers = {
        "Content-Disposition": f"attachment; filename=roster_{assignment.id}.csv"
    }
    return Response(content=data, media_type="text/csv; charset=utf-8", headers=headers)


@router.get("/publisher/evidence", dependencies=[Depends(staff)])
def evidence_chain_page(request: Request, db: Session = Depends(get_db), user=Depends(staff)):
    report = verify_chain(db)
    events = list(db.execute(
        select(EvidenceEvent).order_by(EvidenceEvent.seq.desc()).limit(200)).scalars())
    return templates.TemplateResponse(request, "publisher_evidence.html", base_context(request, user, events=events, report=report))
