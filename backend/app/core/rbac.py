"""Role based access control: permission catalogue and role bindings."""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    AGENT_READ = "agent:read"
    AGENT_EXECUTE = "agent:execute"
    AGENT_WRITE = "agent:write"
    AGENT_PUBLISH = "agent:publish"
    AGENT_LIFECYCLE = "agent:lifecycle"  # pause / resume / disable
    EXECUTION_READ = "execution:read"
    EXECUTION_CANCEL = "execution:cancel"
    TRACE_READ = "trace:read"
    LOG_READ = "log:read"
    METRIC_READ = "metric:read"
    COST_READ = "cost:read"
    COST_ADMIN = "cost:admin"
    APPROVAL_READ = "approval:read"
    APPROVAL_DECIDE = "approval:decide"
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_WRITE = "knowledge:write"
    TOOL_READ = "tool:read"
    TOOL_INVOKE = "tool:invoke"
    SERVICE_READ = "service:read"
    SECURITY_READ = "security:read"
    SECURITY_ADMIN = "security:admin"
    AUDIT_READ = "audit:read"
    USER_ADMIN = "user:admin"
    EVAL_READ = "eval:read"
    EVAL_RUN = "eval:run"
    PLAYGROUND_USE = "playground:use"
    FEATURE_FLAG_ADMIN = "flag:admin"
    CUSTOMER_PII_READ = "customer:pii:read"


READ_ONLY = {
    Permission.AGENT_READ,
    Permission.EXECUTION_READ,
    Permission.TRACE_READ,
    Permission.LOG_READ,
    Permission.METRIC_READ,
    Permission.COST_READ,
    Permission.APPROVAL_READ,
    Permission.KNOWLEDGE_READ,
    Permission.TOOL_READ,
    Permission.SERVICE_READ,
    Permission.EVAL_READ,
}

OPERATOR = READ_ONLY | {
    Permission.AGENT_EXECUTE,
    Permission.AGENT_LIFECYCLE,
    Permission.EXECUTION_CANCEL,
    Permission.TOOL_INVOKE,
    Permission.PLAYGROUND_USE,
    Permission.EVAL_RUN,
}

BUILDER = OPERATOR | {
    Permission.AGENT_WRITE,
    Permission.AGENT_PUBLISH,
    Permission.KNOWLEDGE_WRITE,
}

APPROVER = READ_ONLY | {Permission.APPROVAL_DECIDE, Permission.CUSTOMER_PII_READ}

AUDITOR = READ_ONLY | {Permission.AUDIT_READ, Permission.SECURITY_READ, Permission.COST_READ}

ADMIN = set(Permission)

ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    "admin": ADMIN,
    "platform_engineer": BUILDER | {Permission.SECURITY_READ, Permission.FEATURE_FLAG_ADMIN},
    "agent_builder": BUILDER,
    "operator": OPERATOR,
    "approver": APPROVER,
    "auditor": AUDITOR,
    "analyst": OPERATOR | {Permission.CUSTOMER_PII_READ},
    "viewer": READ_ONLY,
    "service": OPERATOR,
}

ROLE_DESCRIPTIONS: dict[str, str] = {
    "admin": "Full platform administration including users, secrets and budgets.",
    "platform_engineer": "Builds and operates agents, manages feature flags and infrastructure health.",
    "agent_builder": "Creates, versions and publishes agents and knowledge sources.",
    "operator": "Runs agents, manages executions and invokes tools.",
    "approver": "Reviews and decides human-in-the-loop approval requests.",
    "auditor": "Read-only access to audit trail, security posture and cost reporting.",
    "analyst": "Business analyst able to run agents against customer data.",
    "viewer": "Read-only dashboards.",
    "service": "Machine-to-machine identity used by API keys.",
}


def permissions_for_roles(roles: list[str]) -> set[Permission]:
    perms: set[Permission] = set()
    for role in roles:
        perms |= ROLE_PERMISSIONS.get(role, set())
    return perms


def has_permission(roles: list[str], permission: Permission) -> bool:
    return permission in permissions_for_roles(roles)
