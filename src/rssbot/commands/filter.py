"""/filter: a Feed's must-have words and block words. Managers only."""

from __future__ import annotations

import re
from collections.abc import Sequence

import discord
from discord import app_commands

from ..models import Filter, FilterField, FilterList, Level
from . import _ui as ui

EXPLANATION = (
    "An Item is posted when it has at least one must-have word, if there are any, "
    "and no block word. Whole words, capitals ignored. Accents and endings must match exactly: "
    "“cafe” does not match “Café”, “bank” does not match “Banks”.\n"
    "Changes apply to new Items only: an Item that was filtered out is not posted later."
)

LIST_NAMES = {
    FilterList.MUST_HAVE: ("must-have", "Must-have"),
    FilterList.BLOCK: ("block", "Block"),
}
# The lists a form can add to, by the number its custom id carries.
FORM_LISTS = (FilterList.MUST_HAVE, FilterList.BLOCK)

FIELD_CHOICES = (
    (FilterField.ANY.value, "Title and description"),
    (FilterField.TITLE.value, "Title"),
    (FilterField.DESCRIPTION.value, "Description"),
    (FilterField.CATEGORY.value, "Category"),
    (FilterField.AUTHOR.value, "Author"),
)

_FORMATTING = re.compile(r"([\\*_~|`>#\[\]])")

REMOVABLE = ui.CHOICE_LIMIT  # options in one select
NAME_LIMIT = 80
WORD_LIMIT = 100
MORE_LINE = 20  # room kept for "…and 12 more"
NOTICE_LIMIT = 200
ADDED_SHOWN = 3  # added words named in the notice
ADDED_WORD_LIMIT = 40


def _safe(text: str) -> str:
    """Text shown as it is typed: no formatting, no mentions."""
    # A zero-width space after "@" and "<" stops mentions, as discord.utils.escape_mentions does.
    text = text.replace("@", "@​").replace("<", "<​")
    return _FORMATTING.sub(r"\\\1", text)


def _word(flt: Filter) -> str:
    """The word, with its field when it is not "any"."""
    word = _safe(ui.cut(flt.word, WORD_LIMIT))
    return word if flt.field is FilterField.ANY else f"{word} ({flt.field.value})"


def _fit(lines: Sequence[str], budget: int) -> list[str]:
    """The lines, or as many as fit in `budget` characters followed by "…and N more"."""
    if sum(len(line) + 1 for line in lines) <= budget:
        return list(lines)
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget - MORE_LINE:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append(f"…and {len(lines) - len(kept)} more")
    return kept


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _added_notice(added: Sequence[Filter], flt_list: FilterList) -> str:
    """What was stored, so that a line taken as one phrase is seen to be one."""
    start = f"Added {_count(len(added), LIST_NAMES[flt_list][0] + ' word')}: "
    shown = [f"“{_safe(ui.cut(flt.word, ADDED_WORD_LIMIT))}”" for flt in added[:ADDED_SHOWN]]
    while True:
        rest = len(added) - len(shown)
        notice = start + ", ".join(shown) + (f" and {rest} more." if rest else ".")
        if len(notice) <= NOTICE_LIMIT or len(shown) == 1:
            return notice
        shown.pop()


def render_filters(feed_name: str, filters: Sequence[Filter], notice: str | None = None) -> str:
    """The text of the Filters message, always under the message limit."""
    head = [f"**Filters for {_safe(ui.cut(feed_name, NAME_LIMIT))}**", EXPLANATION]
    tail = []
    if notice:
        tail.append(ui.cut(notice, NOTICE_LIMIT))
    if len(filters) > REMOVABLE:
        tail.append(
            f"The menu shows the first {REMOVABLE} words. Removing some will reveal the rest."
        )
    headings = [f"**{LIST_NAMES[flt_list][1]} words**" for flt_list in FORM_LISTS]
    # What is not a list line, with the blank line between every two parts.
    fixed = "\n\n".join([*head, *headings, *tail]) + "\n" * len(headings)
    room = ui.MESSAGE_LIMIT - len(fixed)
    wanted = [
        [f"- {_word(f)}" for f in filters if f.list is flt_list] or ["none"]
        for flt_list in FORM_LISTS
    ]
    # The shorter list is fitted first, so that room it does not need goes to the other.
    sections = [""] * len(FORM_LISTS)
    by_size = sorted(range(len(FORM_LISTS)), key=lambda i: sum(len(line) + 1 for line in wanted[i]))
    for turn, index in enumerate(by_size):
        shown = _fit(wanted[index], room // (len(FORM_LISTS) - turn))
        room -= sum(len(line) + 1 for line in shown)
        sections[index] = headings[index] + "\n" + "\n".join(shown)
    return "\n\n".join([*head, *sections, *tail])


async def _present(interaction: discord.Interaction, content: str, view: discord.ui.View) -> None:
    """Replace the message a control sits on, or send a new private message for a command."""
    from_message = interaction.type in (
        discord.InteractionType.component,
        discord.InteractionType.modal_submit,
    )
    if from_message:
        await ui.edit(interaction, content, view=view)
    else:
        await ui.reply(interaction, content, view=view)


async def open_filters(
    interaction: discord.Interaction, feed_id: int, notice: str | None = None
) -> None:
    """Show the Feed's Filters with the controls to change them.

    Called with a fresh, un-deferred interaction by the /filter command and by the Feed panel's
    button. A button or form interaction has its message replaced; a command gets a new message.
    """
    ui.require_manager(interaction)
    feed = ui.feed_of(interaction, feed_id)
    filters = ui.deps(interaction).service.list_filters(feed.server_id, feed.id)
    content = render_filters(feed.name, filters, notice)
    view = ui.view_of(
        AddMustHave(feed.id),
        AddBlock(feed.id),
        BackToFeed(feed.id),
        RemoveFilter(feed.id, filters=filters[:REMOVABLE]) if filters else None,
    )
    await _present(interaction, content, view)


# -- Buttons and select --


class AddMustHave(ui.ActionButton, action="flt_must", ids=1, requires=Level.MANAGER):
    label = "Add must-have words"
    style = discord.ButtonStyle.primary

    async def handle(self, interaction: discord.Interaction) -> None:
        await _open_form(interaction, self.ids[0], FilterList.MUST_HAVE)


class AddBlock(ui.ActionButton, action="flt_block", ids=1, requires=Level.MANAGER):
    label = "Add block words"
    style = discord.ButtonStyle.primary

    async def handle(self, interaction: discord.Interaction) -> None:
        await _open_form(interaction, self.ids[0], FilterList.BLOCK)


class BackToFeed(ui.ActionButton, action="flt_back", ids=1, requires=Level.MANAGER):
    label = "Back to Feed"

    async def handle(self, interaction: discord.Interaction) -> None:
        from . import feed as feed_commands  # that module may import this one

        feed = ui.feed_of(interaction, self.ids[0])
        await feed_commands.open_panel(interaction, feed.id, edit=True)


class RemoveFilter(ui.ActionSelect, action="flt_remove", ids=1, requires=Level.MANAGER):
    def build(
        self, custom_id: str, *, filters: Sequence[Filter] = ()
    ) -> discord.ui.Select[discord.ui.View]:
        return discord.ui.Select(
            custom_id=custom_id,
            placeholder="Remove a word",
            options=[
                discord.SelectOption(
                    label=ui.cut(flt.word, WORD_LIMIT),
                    value=str(flt.id),
                    description=ui.cut(
                        f"{LIST_NAMES[flt.list][1]}, {_field_name(flt.field)}", WORD_LIMIT
                    ),
                )
                for flt in filters[:REMOVABLE]
            ],
        )

    async def handle(self, interaction: discord.Interaction) -> None:
        picked = self.picked_ids
        if not picked:
            raise ui.UserError("Choose a word to remove.")
        feed = ui.feed_of(interaction, self.ids[0])
        service = ui.deps(interaction).service
        if picked[0] not in {flt.id for flt in service.list_filters(feed.server_id, feed.id)}:
            # Someone else removed it since this message was drawn: show the list as it is now.
            await open_filters(interaction, feed.id, "That word was already removed.")
            return
        removed = await service.remove_filter(
            feed.server_id, feed.id, picked[0], actor=ui.actor_of(interaction)
        )
        word = _safe(ui.cut(removed.word, 60))
        notice = f"Removed “{word}” from the {LIST_NAMES[removed.list][0]} words."
        await open_filters(interaction, feed.id, notice)


def _field_name(field: FilterField) -> str:
    return "title and description" if field is FilterField.ANY else field.value


# -- The form for adding words --


async def _open_form(interaction: discord.Interaction, feed_id: int, flt_list: FilterList) -> None:
    feed = ui.feed_of(interaction, feed_id)
    title = f"Add {LIST_NAMES[flt_list][0]} words"
    await ui.show_form(
        interaction,
        "flt_add",
        feed.id,
        FORM_LISTS.index(flt_list),
        title=title,
        fields=[
            ui.text_field(
                "words",
                "Words",
                long=True,
                placeholder="One word or phrase per line",
                description="One word or phrase per line.",
            ),
            ui.choice_field(
                "field", "Look in", FIELD_CHOICES, default=FilterField.ANY.value, required=True
            ),
        ],
    )


@ui.form_handler("flt_add", ids=2, requires=Level.MANAGER)
async def filters_submitted(
    interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues
) -> None:
    feed_id, list_index = ids
    if list_index >= len(FORM_LISTS):
        raise ui.UserError(ui.STALE)
    flt_list = FORM_LISTS[list_index]
    feed = ui.feed_of(interaction, feed_id)
    try:
        field = FilterField(values.choice("field") or FilterField.ANY.value)
    except ValueError:
        raise ui.UserError("Choose where to look from the list.") from None
    words = values.text("words").splitlines()
    added = await ui.deps(interaction).service.add_filters(
        feed.server_id, feed.id, flt_list, field, words, actor=ui.actor_of(interaction)
    )
    if added:
        notice = _added_notice(added, flt_list)
    else:
        notice = "Nothing was added: the words were blank or already in the list."
    await open_filters(interaction, feed.id, notice)


# -- The command --


@app_commands.command(name="filter", description="Choose the words an Item must or must not have.")
@app_commands.guild_only()
@app_commands.describe(feed="The Feed to filter.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def filter_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    found = ui.feed_from_option(interaction, feed)
    await open_filters(interaction, found.id)


def register(tree: app_commands.CommandTree) -> None:
    tree.add_command(filter_command)
