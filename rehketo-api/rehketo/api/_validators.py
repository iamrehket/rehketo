"""Shared input-validation helpers for the admin/me routers."""

from __future__ import annotations

from rehketo.permissions.check import known_roles

# Slug-like: the name prefixes tool names ({name}__{tool}) on the model's
# tool list, so keep it identifier-safe and stable.  `__` is the separator
# between server name and tool name, so it cannot appear inside a server name
# (server `a__b` + tool `c` would collide with server `a` + tool `b__c`).
# Structure: alnum segments of 1+ chars joined by single _ or - chars, so
# consecutive separators are structurally impossible.  Max length via Field.
NAME_PATTERN = r"^[a-z0-9]+([_-][a-z0-9]+)*$"

_KNOWN_ROLES: frozenset[str] = known_roles()


def validate_roles(roles: list[str]) -> list[str]:
    unknown = sorted(set(roles) - _KNOWN_ROLES)
    if unknown:
        raise ValueError(f"unknown role(s): {', '.join(unknown)}")
    return roles
