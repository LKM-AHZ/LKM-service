import pytest
from sqlalchemy import select

from app.modules.content.blog.models import BlogRepoQuarantine


@pytest.mark.asyncio
async def test_blog_repo_quarantine_table_exists():
    from app.db.base import Base

    table = Base.metadata.tables.get("blog_repo_quarantine")
    assert table is not None
    cols = {c.name for c in table.columns}
    assert {
        "id",
        "repo_name",
        "src_dir",
        "quarantined_at",
        "created_at",
        "updated_at",
    } <= cols
    # repo_name 唯一
    uniq = [c for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"]
    assert any(c.name == "uq_blog_repo_quarantine_repo_name" for c in uniq)


@pytest.mark.asyncio
async def test_blog_repo_quarantine_crud(db):
    row = BlogRepoQuarantine(
        repo_name="pub_abc.git",
        src_dir="/data/blog_repos/pub_abc.git",
        quarantined_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
    )
    db.add(row)
    await db.flush()

    found = (
        (
            await db.execute(
                select(BlogRepoQuarantine).where(
                    BlogRepoQuarantine.repo_name == "pub_abc.git"
                )
            )
        )
        .scalars()
        .first()
    )
    assert found is not None
    assert found.src_dir == "/data/blog_repos/pub_abc.git"
