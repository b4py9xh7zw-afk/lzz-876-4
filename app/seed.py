"""Idempotent demo data: accounts, three red-line quizzes, one opened task."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db
from app.evidence import (
    EV_ASSIGNMENT_OPENED,
    EV_QUIZ_CREATED,
    EV_QUIZ_PUBLISHED,
    EV_USER_CREATED,
    question_digest,
    quiz_snapshot,
    record_event,
)
from app.models import (
    Assignment,
    AssignmentScope,
    Question,
    QuestionType,
    Quiz,
    QuizStatus,
    RiskTier,
    Role,
    User,
)
from app.security import hash_password

DEFAULT_PWD = "compliance123"

ANTI_FRAUD = {
    "title": "反舞弊与商业行为红线测验",
    "category": "anti_fraud",
    "description": "覆盖职务侵占、利益冲突、商业贿赂、举报保护等反舞弊底线要求。",
    "learning_content": (
        "一、公司对舞弊行为零容忍：贪污、受贿、挪用资金、虚报费用、收受供应商回扣均属严重违纪，"
        "涉嫌犯罪的移送司法机关。\n"
        "二、利益冲突必须主动申报：员工本人或近亲属与供应商/客户存在利益关系的，应回避相关业务决策。\n"
        "三、礼品与招待：不得收受现金、现金等价物；超出正常商务礼节的礼品、招待必须登记上交。\n"
        "四、发现可疑行为可通过合规举报渠道匿名举报，公司禁止任何形式的打击报复。\n"
        "五、配合内审调查是全体员工义务，不得隐匿、销毁证据或串供。"
    ),
    "questions": [
        ("single", "某采购员收受供应商 2 万元回扣后将其列入合格供应商名录，该行为属于？",
         ["正常商务往来", "商业贿赂/非国家工作人员受贿，涉嫌犯罪", "行业惯例，无需处理", "只要质量合格就没问题"], [1], 20),
        ("bool", "员工发现同事有舞弊嫌疑时，可以匿名通过合规举报渠道反映。", None, [1], 10),
        ("multiple", "下列哪些情形属于必须申报的利益冲突？",
         ["配偶在投标供应商处任高管", "本人持有竞争方公司股份", "同学在其他行业公司任职且无业务往来", "近亲属参与本部门采购评标"], [0, 1, 3], 20),
        ("single", "关于业务招待与礼品，以下做法正确的是？",
         ["收下供应商赠送的购物卡，不必声张", "现金红包可以收，实物不行", "拒绝现金及现金等价物，超标准礼品登记上交", "部门内部平分即可"], [2], 20),
        ("bool", "内审调查期间，可以删除与调查事项相关的邮件和聊天记录。", None, [0], 10),
        ("multiple", "下列哪些行为属于舞弊？",
         ["虚列差旅费用报销", "伪造合同套取公司资金", "按流程申报真实采购", "利用职务便利将公司业务转给亲属公司"], [0, 1, 3], 20),
    ],
}

DATA_SECURITY = {
    "title": "数据安全与个人信息保护红线测验",
    "category": "data_security",
    "description": "覆盖数据分级、个人信息处理、外发管控、账号与终端安全。",
    "learning_content": (
        "一、公司数据按公开、内部、敏感、核心分级；敏感及以上数据严禁未经审批外发。\n"
        "二、处理客户/员工个人信息须遵循合法、正当、必要和最小化原则，不得超范围收集、私自留存。\n"
        "三、禁止使用个人邮箱、个人网盘、私人即时通讯账号传输工作数据；对外数据共享须签 DPA 并经审批。\n"
        "四、账号不得共用、转借；离岗离职须及时交回权限并注销账号。\n"
        "五、发现数据泄露必须立即上报信息安全部门，不得隐瞒或自行处置。"
    ),
    "questions": [
        ("single", "为方便在家办公，将含客户身份证号的表格上传到个人网盘，属于？",
         ["合理的远程办公方式", "敏感数据违规外发，可能违反《个人信息保护法》", "只要不转发就没事", "经主管口头同意即可"], [1], 20),
        ("multiple", "处理个人信息应遵循哪些原则？",
         ["合法", "正当", "必要、最小化", "能收尽收"], [0, 1, 2], 20),
        ("bool", "为提高协作效率，可以把自己的系统账号密码共享给外包人员使用。", None, [0], 10),
        ("single", "发现可能发生数据泄露时，正确的第一反应是？",
         ["先隐瞒，查清楚再说", "立即上报信息安全部门并保留证据", "自行删除相关文件", "在微信群里讨论"], [1], 20),
        ("bool", "员工离职时，其系统权限应及时回收、账号注销。", None, [1], 10),
        ("multiple", "以下哪些属于敏感/核心数据外发前必须完成的动作？",
         ["履行数据外发审批", "与接收方签署数据处理协议", "脱敏或加密处理", "直接用私人微信发送"], [0, 1, 2], 20),
    ],
}

PROCUREMENT = {
    "title": "采购业务红线测验（草稿）",
    "category": "procurement",
    "description": "三方比价、围标串标识别、合同与验收不相容职责。当前为草稿，仅供出题流程演示。",
    "learning_content": (
        "一、达到招标/比价限额的采购必须至少三家独立报价，不得拆分订单规避招标。\n"
        "二、严禁与供应商串通投标、泄露标底、量身定制技术参数。\n"
        "三、请购、审批、采购、验收、付款为不相容职责，不得由同一人全程包办。\n"
        "四、合同变更与付款必须依据真实验收，严禁虚假验收、提前付款。"
    ),
    "questions": [
        ("single", "将一笔 60 万元的采购拆成三笔 20 万元以规避招标，属于？",
         ["提高效率的做法", "规避招标的违规行为", "主管同意即合规", "只要供应商相同即可"], [1], 25),
        ("multiple", "下列哪些属于采购不相容职责？",
         ["请购与审批", "采购与验收", "验收与付款审核", "同一岗位全流程包办"], [0, 1, 2, 3], 25),
        ("bool", "向意向供应商泄露其他投标方报价，不属于违规。", None, [0], 25),
        ("single", "供应商尚未供货，仅凭其发票就办理全额付款，问题在于？",
         ["没有问题", "违反凭真实验收付款要求，存在资金风险", "财务流程更顺畅", "小额采购都可以"], [1], 25),
    ],
}


def _make_quiz(db: Session, data: dict, creator: User, *, published: bool) -> Quiz:
    quiz = Quiz(
        title=data["title"],
        category=data["category"],
        description=data["description"],
        learning_content=data["learning_content"],
        status=QuizStatus.published if published else QuizStatus.draft,
        pass_score_normal=60,
        pass_score_key=80,
        pass_score_high=90,
        time_limit_minutes=30,
        created_by_id=creator.id,
    )
    db.add(quiz)
    db.flush()
    record_event(
        db, EV_QUIZ_CREATED,
        {"title": quiz.title, "category": quiz.category, "version": quiz.version},
        actor=creator, quiz_id=quiz.id,
    )
    for idx, (qtype, stem, options, answer, score) in enumerate(data["questions"]):
        db.add(Question(
            quiz_id=quiz.id, order_index=idx,
            qtype=QuestionType(qtype), stem=stem,
            options=options, answer=answer, score=score,
        ))
    db.flush()
    if published:
        snap = quiz_snapshot(quiz)
        quiz.question_digest = question_digest(snap)
        quiz.published_at = datetime.utcnow()
        record_event(
            db, EV_QUIZ_PUBLISHED,
            {"snapshot": snap, "question_digest": quiz.question_digest,
             "pass_scores": snap["pass_scores"],
             "published_at": quiz.published_at.isoformat(timespec="seconds")},
            actor=creator, quiz_id=quiz.id,
        )
    return quiz


def seed(db: Session) -> None:
    if db.execute(select(User).limit(1)).scalar_one_or_none() is not None:
        return

    admin = User(employee_no="A000", username="admin", full_name="系统管理员",
                 department="信息技术部", position="管理员", role=Role.admin,
                 risk_tier=RiskTier.normal, password_hash=hash_password(DEFAULT_PWD))
    legal = User(employee_no="L001", username="legal", full_name="李合规",
                 department="法务合规部", position="合规经理", role=Role.publisher,
                 risk_tier=RiskTier.normal, password_hash=hash_password(DEFAULT_PWD))
    audit = User(employee_no="L002", username="audit", full_name="沈审计",
                 department="内部审计部", position="审计经理", role=Role.publisher,
                 risk_tier=RiskTier.normal, password_hash=hash_password(DEFAULT_PWD))
    zhang = User(employee_no="E101", username="zhang", full_name="张敏",
                 department="采购部", position="高级采购工程师", role=Role.employee,
                 risk_tier=RiskTier.high, password_hash=hash_password(DEFAULT_PWD))
    wang = User(employee_no="E102", username="wang", full_name="王强",
                department="数据中心", position="数据库管理员", role=Role.employee,
                risk_tier=RiskTier.high, password_hash=hash_password(DEFAULT_PWD))
    chen = User(employee_no="E103", username="chen", full_name="陈晨",
                department="市场部", position="市场专员", role=Role.employee,
                risk_tier=RiskTier.normal, password_hash=hash_password(DEFAULT_PWD))
    zhao = User(employee_no="E104", username="zhao", full_name="赵磊",
                department="财务部", position="出纳", role=Role.employee,
                risk_tier=RiskTier.key, password_hash=hash_password(DEFAULT_PWD))
    db.add_all([admin, legal, audit, zhang, wang, chen, zhao])
    db.flush()
    for u in (admin, legal, audit, zhang, wang, chen, zhao):
        record_event(db, EV_USER_CREATED, {
            "employee_no": u.employee_no, "username": u.username,
            "full_name": u.full_name, "department": u.department,
            "position": u.position, "role": u.role.value,
            "risk_tier": u.risk_tier.value,
        }, actor=admin)

    q1 = _make_quiz(db, ANTI_FRAUD, legal, published=True)
    q2 = _make_quiz(db, DATA_SECURITY, audit, published=True)
    _make_quiz(db, PROCUREMENT, legal, published=False)

    # 法务向全员开放反舞弊任务；审计向关键岗位及以上开放数据安全任务
    a1 = Assignment(
        quiz_id=q1.id, title="2026年度反舞弊合规学习（全员）",
        audience_scope=AssignmentScope.all, audience_value="",
        opened_at=datetime.utcnow() - timedelta(days=3),
        due_at=datetime.utcnow() + timedelta(days=11),
        created_by_id=legal.id,
    )
    db.add(a1)
    db.flush()
    record_event(db, EV_ASSIGNMENT_OPENED, {
        "title": a1.title, "scope": "all", "audience_value": "",
        "opened_at": a1.opened_at.isoformat(timespec="seconds"),
        "due_at": a1.due_at.isoformat(timespec="seconds") if a1.due_at else None,
    }, actor=legal, quiz_id=q1.id, assignment_id=a1.id)

    a2 = Assignment(
        quiz_id=q2.id, title="数据安全红线学习（关键/高风险岗位）",
        audience_scope=AssignmentScope.risk, audience_value="key",
        opened_at=datetime.utcnow() - timedelta(days=2),
        due_at=datetime.utcnow() + timedelta(days=12),
        created_by_id=audit.id,
    )
    db.add(a2)
    db.flush()
    record_event(db, EV_ASSIGNMENT_OPENED, {
        "title": a2.title, "scope": "risk", "audience_value": "key",
        "opened_at": a2.opened_at.isoformat(timespec="seconds"),
        "due_at": a2.due_at.isoformat(timespec="seconds") if a2.due_at else None,
    }, actor=audit, quiz_id=q2.id, assignment_id=a2.id)

    db.commit()


def run_seed() -> None:
    init_db()
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    run_seed()
    print("seed done")
