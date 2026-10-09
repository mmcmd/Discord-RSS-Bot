"""Who may do what in a Server. Pure: the caller passes in plain facts about the member.

Vocabulary follows CONTEXT.md. When in doubt, deny.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from rssbot.models import Grant, Level, TargetKind


def level_of(
    *,
    user_id: int,
    role_ids: Collection[int],
    is_owner: bool,
    is_administrator: bool,
    grants: Iterable[Grant],
    server_id: int,
) -> Level | None:
    """The member's highest Level in the Server, or None for no access."""
    if is_owner or is_administrator:
        return Level.ADMIN

    best: Level | None = None
    for grant in grants:
        if grant.server_id != server_id:
            continue
        if grant.target_kind is TargetKind.MEMBER:
            held = grant.target_id == user_id
        elif grant.target_kind is TargetKind.ROLE:
            held = grant.target_id in role_ids
        else:
            held = False  # unknown kind: deny
        if not held:
            continue
        if grant.level is Level.ADMIN:
            return Level.ADMIN
        if grant.level is Level.MANAGER:
            best = Level.MANAGER
    return best


def can_admin(level: Level | None) -> bool:
    return level is Level.ADMIN


def can_manage(level: Level | None) -> bool:
    return level is Level.ADMIN or level is Level.MANAGER
