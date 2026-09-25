"""文件库示例数据。用法：uv run python -m app.modules.files.seed

为 library_files 填充社区「文件库」页展示所需的示例元数据（幂等）。
复刻前端原 mock-files 的结构。仅写元数据（不落真实二进制文件），
category_name 由前端根据 category_id 本地映射。
"""

import asyncio
import json
import uuid
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import new_worker_session as new_session
from app.modules.files.models import FileStatus, LibraryFile
from auth import register_models
from auth.entities import Profile, User

register_models()  # 注册 auth ORM 映射类（幂等）

# 种子文件归属的演示上传者用户名（避免依赖具体本地用户）
_SEED_UPLOADER_USERNAME = "file_library_seed_uploader"


async def _ensure_uploader(db: AsyncSession) -> User:
    user = (
        (await db.execute(select(User).where(User.username == _SEED_UPLOADER_USERNAME)))
        .scalars()
        .first()
    )
    if user is None:
        user = User(
            username=_SEED_UPLOADER_USERNAME,
            email=f"{_SEED_UPLOADER_USERNAME}@example.com",
            hashed_password="!seed-only-no-login",  # 不可登录，仅满足 FK
            account_level="local",
        )
        db.add(user)
        await db.flush()
    profile_exists = (
        (await db.execute(select(Profile).where(Profile.user_id == user.id)))
        .scalars()
        .first()
    )
    if profile_exists is None:
        db.add(Profile(user_id=user.id, nickname="文件库运营"))
    return user


class _SeedFileData(TypedDict):
    original_name: str
    mime_type: str
    size: int
    category_id: str
    description: str
    tags: list[str]
    status: FileStatus
    download_count: int
    view_count: int


SEED_FILES: list[_SeedFileData] = [
    {
        "original_name": "天体物理数据集（2026版）.zip",
        "mime_type": "application/zip",
        "size": 134217728,
        "category_id": "physics-astrophysics",
        "description": "涵盖恒星、星系与宇宙学观测的公开数据集，含元数据说明。",
        "tags": ["天体物理", "数据集", "观测"],
        "status": FileStatus.APPROVED,
        "download_count": 230,
        "view_count": 1200,
    },
    {
        "original_name": "量子力学导论_讲义.pdf",
        "mime_type": "application/pdf",
        "size": 5242880,
        "category_id": "physics-quantum",
        "description": "量子力学入门讲义，涵盖波函数、算符与初步应用。",
        "tags": ["量子力学", "讲义", "物理"],
        "status": FileStatus.APPROVED,
        "download_count": 456,
        "view_count": 2300,
    },
    {
        "original_name": "线性代数习题集_详解.pdf",
        "mime_type": "application/pdf",
        "size": 3145728,
        "category_id": "math-linear-algebra",
        "description": "线性代数常见题型与详细解答，适合系统练习。",
        "tags": ["线性代数", "习题", "数学"],
        "status": FileStatus.APPROVED,
        "download_count": 189,
        "view_count": 980,
    },
    {
        "original_name": "有机化学反应机理图解.pdf",
        "mime_type": "application/pdf",
        "size": 8388608,
        "category_id": "chemistry-organic",
        "description": "常用有机反应机理的可视化图解与要点归纳。",
        "tags": ["有机化学", "反应机理", "化学"],
        "status": FileStatus.APPROVED,
        "download_count": 120,
        "view_count": 670,
    },
    {
        "original_name": "Python数据分析实战代码.zip",
        "mime_type": "application/zip",
        "size": 2097152,
        "category_id": "cs-python",
        "description": "Pandas / NumPy 数据分析实战示例代码与说明文档。",
        "tags": ["Python", "数据分析", "代码"],
        "status": FileStatus.APPROVED,
        "download_count": 340,
        "view_count": 1500,
    },
    {
        "original_name": "数学建模竞赛优秀论文集.pdf",
        "mime_type": "application/pdf",
        "size": 15728640,
        "category_id": "math-modeling",
        "description": "历年数学建模竞赛优秀论文选编，含建模思路点评。",
        "tags": ["数学建模", "论文", "竞赛"],
        "status": FileStatus.APPROVED,
        "download_count": 567,
        "view_count": 3200,
    },
    {
        "original_name": "芯片设计入门教程.pdf",
        "mime_type": "application/pdf",
        "size": 12582912,
        "category_id": "ic-design",
        "description": "IC 设计入门学习路径与基础概念讲解。",
        "tags": ["芯片设计", "IC", "教程"],
        "status": FileStatus.PENDING,
        "download_count": 0,
        "view_count": 120,
    },
    {
        "original_name": "英语学术写作指南.pdf",
        "mime_type": "application/pdf",
        "size": 4194304,
        "category_id": "lang-en-writing",
        "description": "面向科研场景的英语学术写作规范与句型参考。",
        "tags": ["英语写作", "学术", "指南"],
        "status": FileStatus.PENDING,
        "download_count": 0,
        "view_count": 85,
    },
]


async def seed_files(db: AsyncSession) -> int:
    count = 0
    uploader = await _ensure_uploader(db)
    for data in SEED_FILES:
        original_name = str(data["original_name"])
        existing = (
            (
                await db.execute(
                    select(LibraryFile).where(
                        LibraryFile.original_name == original_name
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            continue
        db.add(
            LibraryFile(
                uploader_id=uploader.id,
                stored_name=f"{uuid.uuid4().hex}.bin",  # 唯一占位名，无实际物理文件
                original_name=original_name,
                mime_type=data["mime_type"],
                size=data["size"],
                category_id=data["category_id"],
                description=data["description"],
                tags=json.dumps(data["tags"], ensure_ascii=False),
                status=data["status"],
                download_count=data["download_count"],
                view_count=data["view_count"],
            )
        )
        count += 1
    await db.commit()
    return count


async def main() -> None:
    db = await new_session()
    try:
        count = await seed_files(db)
        print(f"seeded {count} files")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
