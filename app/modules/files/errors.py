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
    }
)
