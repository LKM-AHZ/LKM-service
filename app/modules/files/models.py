from enum import StrEnum


class FileStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DELETED = "deleted"


# 文件库模块实际用到/计划的库表及其列（供 /files/status 健康自检展示）。
FILES_TABLE_PLAN = {
    "library_files": [
        "id",
        "uploader_id",
        "original_name",
        "stored_name",
        "sha3_hash",
        "ref_count",
        "storage_path",
        "mime_type",
        "size",
        "category_id",
        "description",
        "tags",
        "status",
        "review_comment",
        "download_count",
        "view_count",
        "created_at",
    ],
}

