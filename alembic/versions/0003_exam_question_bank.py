"""Cq exam question bank, attempt snapshots, review, registration and trials.

Revision ID: 0003_exam_question_bank
Revises: 0002_starhope_sync_receipts

只做加性变更：新表/新列均可空或带默认值，旧考试数据无需回填即可继续使用。
迁移内为既有试卷创建默认题组并分配，把既有题目 group_id 指向该组；create_all
通道由旧读路径（exam_questions.exam_id 回退）兼容，不做启动期回填。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_exam_question_bank"
down_revision: str | Sequence[str] | None = "0002_starhope_sync_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PARTIAL_CERT_INDEX = "uq_exam_certificates_passed_user"


def _uuid_default() -> sa.TextClause:
    return sa.text("public.uuid_generate_v7()")


def upgrade() -> None:
    # ── 新表 ────────────────────────────────────────────────────────────
    op.create_table(
        "exam_question_groups",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "config", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id", sa.Uuid(), server_default=_uuid_default(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )

    op.create_table(
        "exam_group_assignments",
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "id", sa.Uuid(), server_default=_uuid_default(), nullable=False
        ),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"]),
        sa.ForeignKeyConstraint(
            ["group_id"], ["exam_question_groups.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exam_id", "group_id", name="uq_exam_group_assignments"
        ),
    )
    op.create_index(
        "ix_exam_group_assignments_exam_id",
        "exam_group_assignments",
        ["exam_id"],
    )
    op.create_index(
        "ix_exam_group_assignments_group_id",
        "exam_group_assignments",
        ["group_id"],
    )

    op.create_table(
        "exam_attempt_answers",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column(
            "value", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("awarded_points", sa.Numeric(10, 2), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column(
            "id", sa.Uuid(), server_default=_uuid_default(), nullable=False
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["exam_attempts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id", "question_id", name="uq_exam_attempt_answers"
        ),
    )
    op.create_index(
        "ix_exam_attempt_answers_attempt_id",
        "exam_attempt_answers",
        ["attempt_id"],
    )
    op.create_index(
        "ix_exam_attempt_answers_question_id",
        "exam_attempt_answers",
        ["question_id"],
    )

    op.create_table(
        "exam_wrong_answers",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("wrong_count", sa.Integer(), nullable=False),
        sa.Column("last_wrong_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_correct_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("correct_streak", sa.Integer(), nullable=False),
        sa.Column("mastered", sa.Boolean(), nullable=False),
        sa.Column(
            "id", sa.Uuid(), server_default=_uuid_default(), nullable=False
        ),
        sa.ForeignKeyConstraint(["question_id"], ["exam_questions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "question_id", name="uq_exam_wrong_answers"
        ),
    )
    op.create_index(
        "ix_exam_wrong_answers_user_id", "exam_wrong_answers", ["user_id"]
    )
    op.create_index(
        "ix_exam_wrong_answers_question_id",
        "exam_wrong_answers",
        ["question_id"],
    )

    op.create_table(
        "exam_registrations",
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id", sa.Uuid(), server_default=_uuid_default(), nullable=False
        ),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exam_id", "user_id", name="uq_exam_registrations"
        ),
    )
    op.create_index(
        "ix_exam_registrations_exam_id", "exam_registrations", ["exam_id"]
    )
    op.create_index(
        "ix_exam_registrations_user_id", "exam_registrations", ["user_id"]
    )

    op.create_table(
        "exam_trials",
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_published", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "id", sa.Uuid(), server_default=_uuid_default(), nullable=False
        ),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_exam_trials_key"),
    )
    op.create_index("ix_exam_trials_exam_id", "exam_trials", ["exam_id"])

    op.create_table(
        "exam_prerequisites",
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("required_exam_id", sa.Uuid(), nullable=True),
        sa.Column("required_level", sa.String(length=10), nullable=True),
        sa.Column("required_role", sa.String(length=20), nullable=True),
        sa.Column(
            "id", sa.Uuid(), server_default=_uuid_default(), nullable=False
        ),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"]),
        sa.ForeignKeyConstraint(["required_exam_id"], ["exams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exam_id", name="uq_exam_prerequisites_exam"),
    )
    op.create_index(
        "ix_exam_prerequisites_exam_id", "exam_prerequisites", ["exam_id"]
    )

    # ── 既有表加列 ──────────────────────────────────────────────────────
    op.add_column(
        "exams",
        sa.Column(
            "is_listed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "exams",
        sa.Column(
            "is_archived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "exams",
        sa.Column(
            "requires_registration",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("exams", sa.Column("max_attempts", sa.Integer(), nullable=True))
    op.add_column(
        "exams",
        sa.Column(
            "attempt_period_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("365"),
        ),
    )
    op.add_column(
        "exams",
        sa.Column(
            "shuffle_questions",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "exams", sa.Column("random_question_count", sa.Integer(), nullable=True)
    )
    op.add_column(
        "exams",
        sa.Column(
            "config", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    for column in (
        "is_listed",
        "is_archived",
        "requires_registration",
        "attempt_period_days",
        "shuffle_questions",
    ):
        op.alter_column("exams", column, server_default=None)

    op.alter_column(
        "exam_questions", "exam_id", existing_type=sa.Uuid(), nullable=True
    )
    op.add_column(
        "exam_questions",
        sa.Column("group_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_exam_questions_group_id",
        "exam_questions",
        "exam_question_groups",
        ["group_id"],
        ["id"],
    )
    op.add_column(
        "exam_questions", sa.Column("answer_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        "exam_questions",
        sa.Column(
            "grading_mode",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'auto'"),
        ),
    )
    op.add_column(
        "exam_questions",
        sa.Column("source_exam_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_exam_questions_source_exam_id",
        "exam_questions",
        "exams",
        ["source_exam_id"],
        ["id"],
    )
    op.add_column(
        "exam_questions",
        sa.Column(
            "review_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'approved'"),
        ),
    )
    op.add_column(
        "exam_questions",
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "exam_questions",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("exam_questions", "grading_mode", server_default=None)
    op.alter_column("exam_questions", "review_status", server_default=None)
    op.create_index(
        "ix_exam_questions_group_id_sort_order",
        "exam_questions",
        ["group_id", "sort_order"],
    )
    op.create_index(
        "ix_exam_questions_review_status",
        "exam_questions",
        ["review_status"],
    )
    op.create_index(
        "ix_exam_questions_source_exam_id",
        "exam_questions",
        ["source_exam_id"],
    )

    op.add_column(
        "exam_attempts",
        sa.Column(
            "mode",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'certification'"),
        ),
    )
    op.add_column(
        "exam_attempts",
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "exam_attempts",
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "exam_attempts",
        sa.Column(
            "is_trial",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "exam_attempts",
        sa.Column("trial_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_exam_attempts_trial_id",
        "exam_attempts",
        "exam_trials",
        ["trial_id"],
        ["id"],
    )
    op.add_column(
        "exam_attempts",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "exam_attempts", sa.Column("reviewed_by", sa.Uuid(), nullable=True)
    )
    op.alter_column("exam_attempts", "mode", server_default=None)
    op.alter_column("exam_attempts", "is_trial", server_default=None)
    op.create_index(
        "ix_exam_attempts_exam_user_status",
        "exam_attempts",
        ["exam_id", "user_id", "status"],
    )
    op.create_index(
        "ix_exam_attempts_user_started_at",
        "exam_attempts",
        ["user_id", "started_at"],
    )

    # ── 证书去重 + 部分唯一索引 ──────────────────────────────────────────
    # 保留最高分；同分保留最早证书，避免改变旧竞赛榜单。
    op.execute(
        """
        DELETE FROM exam_certificates a
        USING exam_certificates b
        WHERE a.passed AND b.passed
          AND a.exam_id = b.exam_id
          AND a.user_id = b.user_id
          AND (
            a.score < b.score
            OR (
              a.score = b.score
              AND (
                a.issued_at > b.issued_at
                OR (a.issued_at = b.issued_at AND a.id > b.id)
              )
            )
          )
        """
    )
    op.create_index(
        _PARTIAL_CERT_INDEX,
        "exam_certificates",
        ["exam_id", "user_id"],
        unique=True,
        postgresql_where=sa.column("passed").is_(True),
    )

    # ── 旧试卷回填默认题组 ──────────────────────────────────────────────
    op.execute(
        """
        INSERT INTO exam_question_groups
            (id, key, title, description, config, created_at, updated_at)
        SELECT public.uuid_generate_v7(),
               'legacy-' || replace(e.id::text, '-', ''),
               e.title,
               e.description,
               '{}'::jsonb,
               now(),
               now()
        FROM exams e
        WHERE EXISTS (
            SELECT 1 FROM exam_questions q WHERE q.exam_id = e.id
        )
        AND NOT EXISTS (
            SELECT 1 FROM exam_question_groups g WHERE g.key =
                'legacy-' || replace(e.id::text, '-', '')
        )
        """
    )
    op.execute(
        """
        INSERT INTO exam_group_assignments (id, exam_id, group_id, sort_order)
        SELECT public.uuid_generate_v7(), e.id, g.id, 0
        FROM exams e
        JOIN exam_question_groups g
          ON g.key = 'legacy-' || replace(e.id::text, '-', '')
        WHERE NOT EXISTS (
            SELECT 1 FROM exam_group_assignments a WHERE a.exam_id = e.id
        )
        """
    )
    op.execute(
        """
        UPDATE exam_questions q
        SET group_id = g.id
        FROM exams e
        JOIN exam_question_groups g
          ON g.key = 'legacy-' || replace(e.id::text, '-', '')
        WHERE q.exam_id = e.id AND q.group_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index(_PARTIAL_CERT_INDEX, table_name="exam_certificates")

    op.drop_index("ix_exam_attempts_user_started_at", table_name="exam_attempts")
    op.drop_index("ix_exam_attempts_exam_user_status", table_name="exam_attempts")
    op.drop_column("exam_attempts", "reviewed_by")
    op.drop_column("exam_attempts", "reviewed_at")
    op.drop_constraint(
        "fk_exam_attempts_trial_id", "exam_attempts", type_="foreignkey"
    )
    op.drop_column("exam_attempts", "trial_id")
    op.drop_column("exam_attempts", "is_trial")
    op.drop_column("exam_attempts", "snapshot")
    op.drop_column("exam_attempts", "deadline_at")
    op.drop_column("exam_attempts", "mode")

    op.drop_index(
        "ix_exam_questions_source_exam_id", table_name="exam_questions"
    )
    op.drop_index(
        "ix_exam_questions_review_status", table_name="exam_questions"
    )
    op.drop_index(
        "ix_exam_questions_group_id_sort_order", table_name="exam_questions"
    )
    op.drop_column("exam_questions", "deleted_at")
    op.drop_column("exam_questions", "config")
    op.drop_column("exam_questions", "review_status")
    op.drop_constraint(
        "fk_exam_questions_source_exam_id",
        "exam_questions",
        type_="foreignkey",
    )
    op.drop_column("exam_questions", "source_exam_id")
    op.drop_column("exam_questions", "grading_mode")
    op.drop_column("exam_questions", "answer_data")
    op.drop_constraint(
        "fk_exam_questions_group_id", "exam_questions", type_="foreignkey"
    )
    op.drop_column("exam_questions", "group_id")
    op.alter_column(
        "exam_questions", "exam_id", existing_type=sa.Uuid(), nullable=False
    )

    op.drop_column("exams", "config")
    op.drop_column("exams", "random_question_count")
    op.drop_column("exams", "shuffle_questions")
    op.drop_column("exams", "attempt_period_days")
    op.drop_column("exams", "max_attempts")
    op.drop_column("exams", "requires_registration")
    op.drop_column("exams", "is_archived")
    op.drop_column("exams", "is_listed")

    op.drop_index("ix_exam_prerequisites_exam_id", table_name="exam_prerequisites")
    op.drop_table("exam_prerequisites")
    op.drop_index("ix_exam_trials_exam_id", table_name="exam_trials")
    op.drop_table("exam_trials")
    op.drop_index("ix_exam_registrations_user_id", table_name="exam_registrations")
    op.drop_index("ix_exam_registrations_exam_id", table_name="exam_registrations")
    op.drop_table("exam_registrations")
    op.drop_index(
        "ix_exam_wrong_answers_question_id", table_name="exam_wrong_answers"
    )
    op.drop_index("ix_exam_wrong_answers_user_id", table_name="exam_wrong_answers")
    op.drop_table("exam_wrong_answers")
    op.drop_index(
        "ix_exam_attempt_answers_question_id", table_name="exam_attempt_answers"
    )
    op.drop_index(
        "ix_exam_attempt_answers_attempt_id", table_name="exam_attempt_answers"
    )
    op.drop_table("exam_attempt_answers")
    op.drop_index(
        "ix_exam_group_assignments_group_id", table_name="exam_group_assignments"
    )
    op.drop_index(
        "ix_exam_group_assignments_exam_id", table_name="exam_group_assignments"
    )
    op.drop_table("exam_group_assignments")
    op.drop_table("exam_question_groups")
