"""幂等 seed：三级认证考试（无数据时才插入）。竞赛题集待后续阶段补充。

参照 columns/files 的 seed 范式：按标题查重，存在则跳过。用法：
uv run python -m app.modules.exam.seed
"""

import asyncio
import json
from typing import NotRequired, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import new_session
from app.modules.exam.models import Exam, ExamQuestion
from auth import register_models

register_models()  # 注册 auth ORM 映射类（幂等）


class _QuestionSpec(TypedDict):
    """题目规格字段映射到 ExamQuestion 列。"""

    kind: str
    content: str
    options: list[dict[str, str]]
    answer: str
    analysis: NotRequired[str]
    difficulty: NotRequired[int]
    score: int


class _CertExamSpec(TypedDict):
    """考试规格字段映射到 Exam 列（questions 除外）。"""

    type: str
    title: str
    subject: str
    difficulty: int
    description: str
    pass_score: int
    time_limit_min: int
    unlock_level: str | None
    unlock_role: str | None
    starts_at: None
    ends_at: None
    questions: list[_QuestionSpec]


# 三级认证考试（对应 docs/后台管理权限等级需求总结.md 5.1）
_CERT_EXAMS: list[_CertExamSpec] = [
    {
        "type": "exam",
        "title": "初级通识考试",
        "subject": "common",
        "difficulty": 1,
        "description": "平台通用规则（社区公约、发言规范）。通过后解锁 normal 发言（role=member）。",
        "pass_score": 60,
        "time_limit_min": 10,
        "unlock_level": "normal",
        "unlock_role": None,
        "starts_at": None,
        "ends_at": None,
        "questions": [
            {
                "kind": "judge",
                "content": "社区严禁人身攻击与伪科学传播。",
                "options": [],
                "answer": "T",
                "analysis": "见社区公约第3条。",
                "difficulty": 1,
                "score": 20,
            },
            {
                "kind": "single",
                "content": "发现疑似违规内容，社区成员应如何处理？",
                "options": [
                    {"key": "A", "text": "直接开骂"},
                    {"key": "B", "text": "举报或@小组负责人"},
                    {"key": "C", "text": "无视"},
                ],
                "answer": "B",
                "analysis": "正确做法是走举报流程。",
                "difficulty": 1,
                "score": 20,
            },
            {
                "kind": "judge",
                "content": "未认证（local）账户可以在通用板块发帖。",
                "options": [],
                "answer": "F",
                "analysis": "local 仅可浏览、每日限下文件，不可发言。",
                "difficulty": 1,
                "score": 20,
            },
            {
                "kind": "single",
                "content": "专业基础考试通过后可解锁哪项能力？",
                "options": [
                    {"key": "A", "text": "专栏长文（columnist）"},
                    {"key": "B", "text": "全平台发言"},
                    {"key": "C", "text": "后台管理"},
                ],
                "answer": "A",
                "analysis": "专业基础→role=columnist，解锁专栏。",
                "difficulty": 1,
                "score": 20,
            },
        ],
    },
    {
        "type": "exam",
        "title": "专业基础考试（数学）",
        "subject": "math",
        "difficulty": 2,
        "description": "高中数学基础，约会考水平。通过后成为 columnist，解锁专栏。",
        "pass_score": 60,
        "time_limit_min": 20,
        "unlock_level": None,
        "unlock_role": "columnist",
        "starts_at": None,
        "ends_at": None,
        "questions": [
            {
                "kind": "single",
                "content": "下列哪个函数是奇函数？",
                "options": [
                    {"key": "A", "text": "f(x)=x^2"},
                    {"key": "B", "text": "f(x)=x^3"},
                    {"key": "C", "text": "f(x)=|x|"},
                ],
                "answer": "B",
                "analysis": "f(-x)=-x^3=-f(x)，为奇函数。",
                "difficulty": 2,
                "score": 20,
            },
            {
                "kind": "single",
                "content": "sin(30°) 的值是？",
                "options": [
                    {"key": "A", "text": "1/2"},
                    {"key": "B", "text": "√3/2"},
                    {"key": "C", "text": "1"},
                ],
                "answer": "A",
                "analysis": "sin(30°)=1/2。",
                "difficulty": 2,
                "score": 20,
            },
            {
                "kind": "judge",
                "content": "等差数列的通项一定单调递增。",
                "options": [],
                "answer": "F",
                "analysis": "公差可负，d<0 时递减。",
                "difficulty": 2,
                "score": 20,
            },
        ],
    },
    {
        "type": "exam",
        "title": "专业深度考试（数学）",
        "subject": "math",
        "difficulty": 3,
        "description": "本科专业基础，可选进阶难度。通过后成为 author，专栏推荐曝光。",
        "pass_score": 70,
        "time_limit_min": 30,
        "unlock_level": None,
        "unlock_role": "author",
        "starts_at": None,
        "ends_at": None,
        "questions": [
            {
                "kind": "single",
                "content": "微积分中，lim_{x->0}(sin x / x) 的值是？",
                "options": [
                    {"key": "A", "text": "0"},
                    {"key": "B", "text": "1"},
                    {"key": "C", "text": "∞"},
                ],
                "answer": "B",
                "analysis": "经典极限等于 1。",
                "difficulty": 3,
                "score": 25,
            },
            {
                "kind": "single",
                "content": "点 (1,1) 到直线 x+y=0 的距离是？",
                "options": [
                    {"key": "A", "text": "√2"},
                    {"key": "B", "text": "1"},
                    {"key": "C", "text": "2"},
                ],
                "answer": "A",
                "analysis": "|1+1|/√2 = √2。",
                "difficulty": 3,
                "score": 25,
            },
            {
                "kind": "judge",
                "content": "连续函数在某区间上必有界。",
                "options": [],
                "answer": "F",
                "analysis": "连续函数在闭区间上有界；开区间不保证。",
                "difficulty": 3,
                "score": 25,
            },
        ],
    },
]


async def seed_exams(db: AsyncSession) -> int:
    """幂等 seed：按标题查重，插入缺失的考试与题目，返回新建条数。"""
    created = 0
    for spec in _CERT_EXAMS:
        exists = await db.scalar(select(Exam.id).where(Exam.title == spec["title"]))
        if exists is not None:
            continue
        questions = spec["questions"]
        # 显式 is_published=True：模型默认 False，而 start_attempt 会拒绝未发布考试，
        # 且本模块没有任何发布/更新端点 → 种子考试若留在默认值就永远无法作答
        exam = Exam(
            is_published=True,
            **{k: v for k, v in spec.items() if k != "questions"},
        )
        db.add(exam)
        await db.flush()
        for qi in questions:
            db.add(
                ExamQuestion(
                    exam_id=exam.id,
                    kind=qi["kind"],
                    content=qi["content"],
                    options=json.dumps(qi["options"], ensure_ascii=False),
                    answer=qi["answer"],
                    analysis=qi.get("analysis"),
                    difficulty=qi.get("difficulty", 1),
                    score=qi["score"],
                )
            )
        await db.flush()
        created += 1
    return created


async def main() -> None:
    """CLI 入口（与 files/columns 的 seed 范式一致）：独立会话灌入后统一提交。"""
    db = await new_session()
    try:
        created = await seed_exams(db)
        # seed_exams 只用 flush（供测试回滚），CLI 灌入需落库，故此处提交
        await db.commit()
        print(f"seeded {created} exams")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
