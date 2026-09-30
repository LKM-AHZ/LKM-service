from core.err import NS_STORAGE, ErrCode, register


class StorageErr(ErrCode):
    NOT_FOUND = NS_STORAGE.err(1)
    STORE_ERROR = NS_STORAGE.err(2)
    TOO_LARGE = NS_STORAGE.err(3)


register(
    {
        StorageErr.NOT_FOUND: (404, "Storage key not found"),
        StorageErr.STORE_ERROR: (500, "Storage operation failed"),
        StorageErr.TOO_LARGE: (413, "Content exceeds storage size limit"),
    }
)
