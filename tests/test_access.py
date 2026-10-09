from __future__ import annotations

import pytest

from rssbot.access import can_admin, can_manage, level_of
from rssbot.models import Grant, Level, TargetKind

SERVER = 100
OTHER_SERVER = 200
USER = 1
ROLE = 2
OTHER_ROLE = 3


def grant(target_id: int, kind: TargetKind, level: Level, server_id: int = SERVER) -> Grant:
    return Grant(server_id=server_id, target_id=target_id, target_kind=kind, level=level)


def level(
    *,
    user_id: int = USER,
    role_ids: tuple[int, ...] = (ROLE,),
    is_owner: bool = False,
    is_administrator: bool = False,
    grants: tuple[Grant, ...] = (),
) -> Level | None:
    return level_of(
        user_id=user_id,
        role_ids=role_ids,
        is_owner=is_owner,
        is_administrator=is_administrator,
        grants=grants,
        server_id=SERVER,
    )


def test_owner_is_admin() -> None:
    assert level(is_owner=True) is Level.ADMIN


def test_administrator_permission_is_admin() -> None:
    assert level(is_administrator=True) is Level.ADMIN


def test_admin_by_role() -> None:
    assert level(grants=(grant(ROLE, TargetKind.ROLE, Level.ADMIN),)) is Level.ADMIN


def test_admin_by_member() -> None:
    assert level(grants=(grant(USER, TargetKind.MEMBER, Level.ADMIN),)) is Level.ADMIN


def test_manager_by_role() -> None:
    assert level(grants=(grant(ROLE, TargetKind.ROLE, Level.MANAGER),)) is Level.MANAGER


def test_manager_by_member() -> None:
    assert level(grants=(grant(USER, TargetKind.MEMBER, Level.MANAGER),)) is Level.MANAGER


@pytest.mark.parametrize("reverse", [False, True])
def test_highest_level_wins_regardless_of_order(reverse: bool) -> None:
    grants = [
        grant(ROLE, TargetKind.ROLE, Level.MANAGER),
        grant(USER, TargetKind.MEMBER, Level.ADMIN),
    ]
    if reverse:
        grants.reverse()
    assert level(grants=tuple(grants)) is Level.ADMIN


def test_manager_grant_does_not_hide_admin_from_owner() -> None:
    grants = (grant(USER, TargetKind.MEMBER, Level.MANAGER),)
    assert level(is_owner=True, grants=grants) is Level.ADMIN


def test_nothing_means_no_access() -> None:
    assert level() is None


def test_grant_for_unheld_role_or_other_member_is_ignored() -> None:
    grants = (
        grant(OTHER_ROLE, TargetKind.ROLE, Level.ADMIN),
        grant(999, TargetKind.MEMBER, Level.ADMIN),
    )
    assert level(grants=grants) is None


def test_grant_from_another_server_is_ignored() -> None:
    grants = (
        grant(ROLE, TargetKind.ROLE, Level.ADMIN, server_id=OTHER_SERVER),
        grant(USER, TargetKind.MEMBER, Level.ADMIN, server_id=OTHER_SERVER),
    )
    assert level(grants=grants) is None


def test_other_server_grant_does_not_outrank_this_servers_grant() -> None:
    grants = (
        grant(USER, TargetKind.MEMBER, Level.ADMIN, server_id=OTHER_SERVER),
        grant(USER, TargetKind.MEMBER, Level.MANAGER),
    )
    assert level(grants=grants) is Level.MANAGER


def test_role_grant_never_matches_a_user_id() -> None:
    # The role's id equals the member's user id, but the member does not hold the role.
    grants = (grant(USER, TargetKind.ROLE, Level.ADMIN),)
    assert level(role_ids=(OTHER_ROLE,), grants=grants) is None


def test_member_grant_never_matches_a_role_id() -> None:
    # The member holds a role whose id equals the Grant's target, but they are not that user.
    grants = (grant(ROLE, TargetKind.MEMBER, Level.ADMIN),)
    assert level(user_id=USER, role_ids=(ROLE,), grants=grants) is None


def test_colliding_ids_still_match_the_right_kind() -> None:
    both = (
        grant(5, TargetKind.ROLE, Level.MANAGER),
        grant(5, TargetKind.MEMBER, Level.ADMIN),
    )
    # Holds role 5 only: the Manager role Grant applies, the member Grant does not.
    assert level(user_id=USER, role_ids=(5,), grants=both) is Level.MANAGER
    # Is user 5 only: the Admin member Grant applies.
    assert level(user_id=5, role_ids=(), grants=both) is Level.ADMIN


def test_empty_inputs() -> None:
    assert level(role_ids=(), grants=()) is None


def test_no_roles_ignores_role_grants() -> None:
    assert level(role_ids=(), grants=(grant(ROLE, TargetKind.ROLE, Level.ADMIN),)) is None


def test_accepts_any_collection_and_iterable() -> None:
    result = level_of(
        user_id=USER,
        role_ids={ROLE},
        is_owner=False,
        is_administrator=False,
        grants=iter([grant(ROLE, TargetKind.ROLE, Level.MANAGER)]),
        server_id=SERVER,
    )
    assert result is Level.MANAGER


@pytest.mark.parametrize(
    ("lvl", "admin", "manage"),
    [
        (Level.ADMIN, True, True),
        (Level.MANAGER, False, True),
        (None, False, False),
    ],
)
def test_capability_checks(lvl: Level | None, admin: bool, manage: bool) -> None:
    assert can_admin(lvl) is admin
    assert can_manage(lvl) is manage
