"""跨 auth/业务域共享的 RBAC 角色层级与职责分离规则。"""

import json
import os
from dataclasses import dataclass

KNOWN_ROLES = frozenset(
    {
        "local:member",
        "normal:member",
        "normal:columnist",
        "normal:author",
        "admin:incubated_member",
        "admin:org_member",
        "admin:content_reviewer",
        "admin:content_publisher",
        "admin:super_admin",
    }
)


# senior -> directly inherited junior roles. Policy edges stay within one level.
ROLE_INHERITANCE: dict[str, frozenset[str]] = {
    "normal:columnist": frozenset({"normal:member"}),
    "normal:author": frozenset({"normal:columnist"}),
    "admin:org_member": frozenset({"admin:incubated_member"}),
    "admin:super_admin": frozenset({"admin:org_member"}),
}


@dataclass(frozen=True)
class SeparationConstraint:
    roles: frozenset[str]
    max_roles: int


def _load_constraints(name: str) -> tuple[SeparationConstraint, ...]:
    """Load deployment policy from a JSON array of {roles,max_roles} objects."""
    raw = os.getenv(name, "[]")
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("Policy must be a JSON array")
        constraints = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"roles", "max_roles"}:
                raise ValueError("Invalid constraint object")
            roles = item["roles"]
            maximum = item["max_roles"]
            if (
                not isinstance(roles, list)
                or any(not isinstance(role, str) for role in roles)
                or len(roles) != len(set(roles))
                or not set(roles) <= KNOWN_ROLES
                or not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or not 0 < maximum < len(roles)
            ):
                raise ValueError("Invalid constraint roles or cardinality")
            constraints.append(SeparationConstraint(frozenset(roles), maximum))
        return tuple(constraints)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {name}") from exc


# Existing business roles do not express conflicting duties, so the default policy
# is empty. Operators can declare actual conflicts identically in auth deployments.
SSD_CONSTRAINTS = _load_constraints("LKM_RBAC_SSD_CONSTRAINTS")
DSD_CONSTRAINTS = _load_constraints("LKM_RBAC_DSD_CONSTRAINTS")


def _validate_hierarchy() -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(role: str) -> None:
        if role in visiting:
            raise ValueError(f"Role inheritance cycle at {role}")
        if role in visited:
            return
        visiting.add(role)
        for junior in ROLE_INHERITANCE.get(role, ()):
            if role not in KNOWN_ROLES or junior not in KNOWN_ROLES:
                raise ValueError("Role inheritance references unknown role")
            if role.split(":", 1)[0] != junior.split(":", 1)[0]:
                raise ValueError("Role inheritance crosses account levels")
            visit(junior)
        visiting.remove(role)
        visited.add(role)

    for role in ROLE_INHERITANCE:
        visit(role)


_validate_hierarchy()


def role_closure(roles: set[str] | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Include every known role's transitive juniors."""
    result: set[str] = set()
    pending = [role for role in roles if role in KNOWN_ROLES]
    while pending:
        role = pending.pop()
        if role in result:
            continue
        result.add(role)
        pending.extend(ROLE_INHERITANCE.get(role, ()))
    return tuple(sorted(result))


def satisfies_constraints(
    roles: set[str] | tuple[str, ...] | list[str],
    constraints: tuple[SeparationConstraint, ...],
) -> bool:
    """Check cardinalities against the authorized closure, including hierarchy."""
    closure = set(role_closure(roles))
    for item in constraints:
        if not item.roles <= KNOWN_ROLES or not 0 < item.max_roles < len(item.roles):
            raise ValueError("Invalid separation constraint")
        if len(closure & item.roles) > item.max_roles:
            return False
    return True


def composite_role(account_level: str, role: str) -> str:
    return f"{account_level}:{role}"


def session_roles_claim(value: object) -> tuple[str, ...] | None:
    """Parse the optional signed JWT role selection without coercing malformed data."""
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(role, str) for role in value):
        raise ValueError("Malformed active_roles claim")
    return tuple(value)


def assigned_roles(
    account_level: str, primary_role: str, assigned_roles: list[str] | tuple[str, ...]
) -> tuple[str, ...]:
    """Direct UA plus the legacy primary role, excluding other levels."""
    prefix = f"{account_level}:"
    roles = {composite_role(account_level, primary_role)}
    roles.update(
        role
        for role in assigned_roles
        if role.startswith(prefix) and role in KNOWN_ROLES
    )
    return tuple(sorted(roles))


def authorized_roles(
    account_level: str, primary_role: str, extra_roles: list[str] | tuple[str, ...]
) -> tuple[str, ...]:
    """Directly assigned roles and every junior they authorize (RBAC1)."""
    return role_closure(assigned_roles(account_level, primary_role, extra_roles))


def activated_roles(
    account_level: str,
    primary_role: str,
    extra_roles: list[str] | tuple[str, ...],
    selected_roles: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Activate a selected subset; legacy tokens activate all assigned roles."""
    direct = assigned_roles(account_level, primary_role, extra_roles)
    available = role_closure(direct)
    if not satisfies_constraints(available, SSD_CONSTRAINTS):
        raise ValueError("Assigned roles violate SSD")
    if selected_roles is None:
        # Preserve existing all-active behavior when possible. On a DSD conflict,
        # try direct assignments first, then inherited juniors. A senior whose
        # closure violates DSD may still authorize a safe junior session.
        chosen: list[str] = []
        candidates = (
            composite_role(account_level, primary_role),
            *direct,
            *available,
        )
        for role in candidates:
            if role not in role_closure(chosen) and satisfies_constraints(
                [*chosen, role], DSD_CONSTRAINTS
            ):
                chosen.append(role)
        selected = tuple(sorted(chosen))
    else:
        if len(selected_roles) != len(set(selected_roles)) or not set(
            selected_roles
        ) <= set(available):
            raise ValueError("Session roles are not authorized")
        selected = tuple(sorted(selected_roles))
    if not satisfies_constraints(selected, DSD_CONSTRAINTS):
        raise ValueError("Session roles violate DSD")
    return selected
