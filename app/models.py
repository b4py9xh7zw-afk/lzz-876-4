"""Database models for the compliance assessment platform.

Design goals
------------
* Every meaningful action (open learning page, start/submit quiz, sign
  confirmation, publish/close, admin changes) is mirrored into
  ``EvidenceEvent`` rows by ``app.evidence`` — that table is the append-only,
  hash-chained audit ledger.
* Quiz questions are *snapshotted* into each Attempt at start time, so later
  edits of the master quiz cannot change historical records.
* Pass thresholds are recorded per attempt (``required_pass_score``) because
  the rule depends on the employee's risk tier at the time of taking.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import JSON

from app.database import Base


class Role(str, enum.Enum):
    employee = "employee"
    publisher = "publisher"   # 法务 / 审计
    admin = "admin"


class RiskTier(str, enum.Enum):
    normal = "normal"   # 普通岗位
    key = "key"         # 关键岗位
    high = "high"       # 高风险岗位（硬门槛）


class QuizStatus(str, enum.Enum):
    draft = "draft"
    published = "published"
    closed = "closed"


class QuestionType(str, enum.Enum):
    single = "single"      # 单选
    multiple = "multiple"  # 多选（少选/错选不得分）
    bool = "bool"          # 判断


class AttemptStatus(str, enum.Enum):
    in_progress = "in_progress"
    submitted = "submitted"


class AssignmentScope(str, enum.Enum):
    all = "all"                       # 全员
    department = "department"         # 指定部门
    risk = "risk"                     # 指定风险等级及以上
    individual = "individual"         # 指定人员


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(64))
    department: Mapped[str] = mapped_column(String(64), default="")
    position: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.employee)
    risk_tier: Mapped[RiskTier] = mapped_column(Enum(RiskTier), default=RiskTier.normal)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Quiz(Base):
    __tablename__ = "quizzes"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    learning_content: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[QuizStatus] = mapped_column(Enum(QuizStatus), default=QuizStatus.draft)
    # 不同岗位风险等级的及格线（百分制）
    pass_score_normal: Mapped[int] = mapped_column(Integer, default=60)
    pass_score_key: Mapped[int] = mapped_column(Integer, default=80)
    pass_score_high: Mapped[int] = mapped_column(Integer, default=90)
    time_limit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 发布时题目清单的摘要（题面+答案），用于事后检测题库是否被改
    question_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_by: Mapped[User] = relationship()
    questions: Mapped[list["Question"]] = relationship(
        back_populates="quiz", cascade="all, delete-orphan", order_by="Question.order_index"
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("quizzes.id"), index=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    qtype: Mapped[QuestionType] = mapped_column(Enum(QuestionType))
    stem: Mapped[str] = mapped_column(Text)
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    answer: Mapped[list] = mapped_column(JSON)   # 正确选项下标列表
    score: Mapped[int] = mapped_column(Integer, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    quiz: Mapped[Quiz] = relationship(back_populates="questions")


class Assignment(Base):
    """一次正式发布的学习/测评任务（来自测验）。"""
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("quizzes.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    audience_scope: Mapped[AssignmentScope] = mapped_column(Enum(AssignmentScope))
    audience_value: Mapped[str] = mapped_column(String(255), default="")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    quiz: Mapped[Quiz] = relationship()
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("quizzes.id"), index=True)
    assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id"), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    quiz_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[AttemptStatus] = mapped_column(
        Enum(AttemptStatus), default=AttemptStatus.in_progress
    )
    question_snapshot: Mapped[list] = mapped_column(JSON)
    answers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    grading: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_score: Mapped[int] = mapped_column(Integer, default=100)
    required_pass_score: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    client_ua: Mapped[str] = mapped_column(String(300), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship()
    quiz: Mapped[Quiz] = relationship()
    confirmation: Mapped["Confirmation | None"] = relationship(
        back_populates="attempt", uselist=False
    )


class Confirmation(Base):
    """学习确认 / 合规承诺签署（签署后不可改）。"""
    __tablename__ = "confirmations"
    __table_args__ = (UniqueConstraint("attempt_id", name="uq_confirmation_attempt"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("quizzes.id"), index=True)
    employee_no: Mapped[str] = mapped_column(String(32))
    full_name: Mapped[str] = mapped_column(String(64))
    statement: Mapped[str] = mapped_column(Text)
    signature_text: Mapped[str] = mapped_column(String(120))
    signature_hash: Mapped[str] = mapped_column(String(64))
    score: Mapped[int] = mapped_column(Integer)
    required_pass_score: Mapped[int] = mapped_column(Integer)
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    client_ua: Mapped[str] = mapped_column(String(300), default="")
    signed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    attempt: Mapped[Attempt] = relationship(back_populates="confirmation")


class EvidenceEvent(Base):
    """追加式证据链：每行包含前一行哈希，形成哈希链。

    event_hash = sha256(seq | event_type | actor | refs | canonical(payload)
                        | created_at_iso | prev_hash)
    """
    __tablename__ = "evidence_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_employee_no: Mapped[str] = mapped_column(String(32), default="")
    actor_name: Mapped[str] = mapped_column(String(64), default="")
    quiz_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    assignment_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    attempt_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    confirmation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    client_ua: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
