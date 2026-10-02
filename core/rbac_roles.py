"""跨 auth/业务域共享的会话角色规则。"""

KNOWN_ROLES = frozenset(
    {
        "local:member",
        "normal:member",
        "normal:columnist",
        "normal:author",
        "admin:incubated_member",
        "admin:org_member",
        "admin:super_admin",
    }
)


def composite_role(account_level: str, role: str) -> str:
    return f"{account_level}:{role}"


def activated_roles(
    account_level: str, primary_role: str, assigned_roles: list[str] | tuple[str, ...]
) -> tuple[str, ...]:
    """每个会话激活当前等级下全部已分配角色，含兼容旧数据的基础角色。"""
    prefix = f"{account_level}:"
    roles = {composite_role(account_level, primary_role)}
    roles.update(
        role
        for role in assigned_roles
        if role.startswith(prefix) and role in KNOWN_ROLES
    )
    return tuple(sorted(roles))
