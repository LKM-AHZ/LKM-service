from core.err import NS_STARHOPE, ErrCode, register


class StarHopeErr(ErrCode):
    NOT_FOUND = NS_STARHOPE.err(1)
    INVALID_ENTITY = NS_STARHOPE.err(2)


register(
    {
        StarHopeErr.NOT_FOUND: (404, "StarHope entity not found"),
        StarHopeErr.INVALID_ENTITY: (422, "Invalid StarHope entity type"),
    }
)
