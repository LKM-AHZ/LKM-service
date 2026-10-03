from core.err import NS_FILES, ErrCode, register


class FileErr(ErrCode):
    NOT_FOUND = NS_FILES.err(1)
    STORE_ERROR = NS_FILES.err(2)
    TOO_LARGE = NS_FILES.err(3)
    INVALID_STATUS = NS_FILES.err(4)
    NOT_PENDING = NS_FILES.err(5)
    NOT_APPROVED = NS_FILES.err(6)
    UPLOAD_NOT_FOUND = NS_FILES.err(7)
    UPLOAD_EXPIRED = NS_FILES.err(8)
    NOT_OWNER = NS_FILES.err(9)
    INVALID_PROJECT = NS_FILES.err(10)
    UNSAFE_CONTENT = NS_FILES.err(11)
    SCAN_UNAVAILABLE = NS_FILES.err(12)
    PREVIEW_UNAVAILABLE = NS_FILES.err(13)


register(
    {
        FileErr.NOT_FOUND: (404, "File not found"),
        FileErr.STORE_ERROR: (500, "File storage operation failed"),
        FileErr.TOO_LARGE: (413, "File exceeds upload size limit"),
        FileErr.INVALID_STATUS: (400, "Invalid file status transition"),
        FileErr.NOT_PENDING: (409, "File is not in pending status"),
        FileErr.NOT_APPROVED: (403, "File is not approved for download or preview"),
        FileErr.UPLOAD_NOT_FOUND: (
            409,
            "Upload target not found (direct upload failed)",
        ),
        FileErr.UPLOAD_EXPIRED: (410, "Upload session expired, please re-initiate"),
        FileErr.NOT_OWNER: (403, "Only the document owner can add a version"),
        FileErr.INVALID_PROJECT: (
            400,
            "Project does not exist or user is not a member",
        ),
        FileErr.UNSAFE_CONTENT: (422, "File failed security screening"),
        FileErr.SCAN_UNAVAILABLE: (503, "File scanner is unavailable"),
        FileErr.PREVIEW_UNAVAILABLE: (415, "Document preview is unavailable"),
    }
)
