from app.core.err import NS_INTERACTION, ErrCode, register


class InteractionErr(ErrCode):
    CONTENT_NOT_FOUND = NS_INTERACTION.err(1)


register(
    {
        InteractionErr.CONTENT_NOT_FOUND: (404, "内容不存在"),
    }
)
