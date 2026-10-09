"""The Discord slash commands. See PATTERNS.md for how a command module is written."""

from __future__ import annotations

import discord
from discord import app_commands

from . import _ui, access, feed, filter, help, log, setup, template

# ---------------------------------------------------------------------------------------
# Every command module, in the order its commands are added. ADD NEW MODULES HERE.
# Each one exposes `register(tree)`; importing it is what registers its buttons, selects
# and forms.
# ---------------------------------------------------------------------------------------
MODULES = (setup, access, feed, template, filter, log, help)


def add_all(tree: app_commands.CommandTree, client: discord.Client) -> None:
    """Add every command to the tree and wire up error handling, components and forms.

    The start-up file calls this once, after setting `client.deps`.
    """
    tree.error(_ui.on_tree_error)
    for module in MODULES:
        module.register(tree)
    _ui.register_dynamic_items(client)
