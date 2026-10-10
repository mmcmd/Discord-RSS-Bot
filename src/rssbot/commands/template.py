"""/template: a Feed's Template: its message text, Embed, Fields and Buttons. Managers only.

The Feed panel opens the same forms and lists through open_text(), open_embed(),
open_fields() and open_buttons().
"""

from __future__ import annotations

import zlib
from collections.abc import Callable, Sequence
from typing import Any

import discord
from discord import app_commands

from ..deliver import build_embed, build_view
from ..models import MAX_BUTTONS, MAX_EMBED_FIELDS, ButtonSpec, EmbedSpec, Feed, FieldSpec, Level
from ..render import (
    MAX_BUTTON_LABEL,
    MAX_BUTTON_URL,
    MAX_CONTENT,
    MAX_EMBED_FOOTER,
    MAX_EMBED_TITLE,
    MAX_EMBED_URL,
    MAX_FIELD_NAME,
    MAX_FIELD_VALUE,
    hidden_buttons,
)
from ..service import ServiceError
from ..template import ADDRESS_PLACEHOLDERS, TemplateError, leading_names, validate
from . import _ui as ui

HINT = "Placeholders like {{title}} work here."
TEXT_HINT = "{{title}} and other Placeholders work. Leave empty to post only the Embed."
URL_HINT = "A web address. Placeholders like {{link}} work here."

# The colour option travels in the Embed form's custom id as one non-negative integer.
KEEP_COLOUR = 0
NO_COLOUR = 1
_COLOUR_OFFSET = 2
NO_COLOUR_VALUE = "none"
BAD_COLOUR = "The colour must be a hex code such as #ff8800, or none for no colour."

COLOURS: tuple[tuple[str, int], ...] = (
    ("Red", 0xE74C3C),
    ("Orange", 0xE67E22),
    ("Yellow", 0xF1C40F),
    ("Green", 0x2ECC71),
    ("Teal", 0x1ABC9C),
    ("Blue", 0x3498DB),
    ("Purple", 0x9B59B6),
    ("Pink", 0xE91E63),
    ("Grey", 0x95A5A6),
    ("Dark", 0x23272A),
)

PREVIEW_REFUSED = (
    "The preview could not be shown: Discord refused it, "
    "so an Item posted with this Template may be refused too."
)
BAD_ADDRESS = (
    " must start with http://, https:// or one of the Placeholders that hold a web address: "
    + ", ".join(f"{{{{{name}}}}}" for name in ADDRESS_PLACEHOLDERS[:-1])
    + f" or {{{{{ADDRESS_PLACEHOLDERS[-1]}}}}}."
)
BUTTONS_HIDDEN = "not shown for this Item: the label is empty or the address is not a web address."
LIST_CHANGED = "The list has changed. Choose again."
NO_ITEM_VALUES = "The Feed's newest Item could not be read just now, so no values are shown."
PLACEHOLDER_NOTES = (
    "Fallback: `{{summary||description}}` uses the first of them that is not empty.",
    "Length limit: `{{description:200}}` cuts the value to 200 characters.",
    "`{{url}}` is another name for `{{link}}`.",
)

Controls = Callable[[], Sequence[discord.ui.Item[Any]]]


# -- Helpers --


def _service(interaction: discord.Interaction) -> Any:
    return ui.deps(interaction).service


def _name(feed: Feed) -> str:
    return f"**{discord.utils.escape_markdown(ui.cut(feed.name, 80))}**"


def _from_message(interaction: discord.Interaction) -> bool:
    """Whether the interaction sits on a message: a component, or a form opened by one."""
    if interaction.type is discord.InteractionType.component:
        return True
    return (
        interaction.type is discord.InteractionType.modal_submit
        and getattr(interaction, "message", None) is not None
    )


async def _show(
    interaction: discord.Interaction,
    content: str,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Replace the message the interaction came from, or reply if it came from a command."""
    if _from_message(interaction):
        await ui.edit(interaction, content, embed=embed, view=view)
    else:
        await ui.reply(interaction, content, embed=embed, view=view)


async def _defer(interaction: discord.Interaction) -> None:
    await ui.defer(interaction, update=_from_message(interaction))


def _share(texts: Sequence[str], room: int) -> list[str]:
    """Cut the texts so that together they fit in `room`, shortening the longest most."""
    out = list(texts)
    order = sorted(range(len(out)), key=lambda index: len(out[index]))
    for done, index in enumerate(order):
        out[index] = ui.cut(out[index], max(room, 0) // (len(out) - done))
        room -= len(out[index])
    return out


def _one_line(text: str, limit: int) -> str:
    """A Template string as a short piece of inline code."""
    return f"`{ui.cut(' '.join(text.split()), limit).replace('`', "'")}`"


def _check_address(what: str, text: str) -> None:
    """Refuse an address Template that could not be a web address for any Item."""
    text = text.strip()
    if not text or text.lower().startswith(("http://", "https://")):
        return
    try:
        validate(text)
    except TemplateError:
        return  # saving it says which Placeholder is wrong
    names = leading_names(text)
    if not names or any(name not in ADDRESS_PLACEHOLDERS for name in names):
        raise ui.UserError(what + BAD_ADDRESS)


def _mark(*parts: str) -> str:
    """A short checksum of a list entry, to notice a pick made in a list that has changed."""
    return f"{zlib.crc32(chr(0).join(parts).encode('utf-8', 'replace')):08x}"


def _picked_entry(picked: Sequence[str]) -> tuple[int, str] | None:
    """The position and checksum in the value of a remove option, or None if it is not one."""
    number, _, mark = next(iter(picked), "").partition(":")
    if not (number.isascii() and number.isdigit() and len(number) <= 4):
        return None
    return int(number), mark


def _hidden_note(positions: Sequence[int]) -> str:
    """One line naming the Buttons left out of a preview, or "" if there are none."""
    if not positions:
        return ""
    if len(positions) == 1:
        return f"Button {positions[0]} is {BUTTONS_HIDDEN}\n"
    numbers = [str(position) for position in positions]
    return f"Buttons {', '.join(numbers[:-1])} and {numbers[-1]} are {BUTTONS_HIDDEN}\n"


def _colour_code(option: str | None) -> int:
    """The colour option as the integer the Embed form carries."""
    if option is None:
        return KEEP_COLOUR
    text = option.strip().lower()
    if text == NO_COLOUR_VALUE:
        return NO_COLOUR
    text = text.removeprefix("#")
    if len(text) != 6 or any(c not in "0123456789abcdef" for c in text):
        raise ui.UserError(BAD_COLOUR)
    return int(text, 16) + _COLOUR_OFFSET


def _colour_of(code: int) -> int | str | None:
    """What to hand FeedService.set_embed: None keeps the colour and "" clears it."""
    if code == KEEP_COLOUR:
        return None
    if code == NO_COLOUR:
        return ""
    return code - _COLOUR_OFFSET


async def _result(
    interaction: discord.Interaction, feed_id: int, headline: str, controls: Controls
) -> None:
    """Confirm a change and preview the newest Item. The interaction is already deferred."""
    try:
        message, item = await _service(interaction).preview(ui.server_id_of(interaction), feed_id)
    except ServiceError as exc:
        content = f"{headline}\nThe preview could not be shown: {exc}"
        await _show(interaction, content, view=ui.view_of(*controls()))
        return
    hidden = _hidden_note(hidden_buttons(ui.feed_of(interaction, feed_id), item))
    head = f"{headline}\n{hidden}**Preview** of the newest Item as it would be posted:\n"
    links = build_view(message)
    link_buttons = list(links.children) if links is not None else []
    for button in link_buttons:
        button.row = 4  # under the controls, never mixed in with them
    try:
        await _show(
            interaction,
            head + ui.cut(message.content, ui.MESSAGE_LIMIT - len(head)),
            embed=build_embed(message.embed, published=message.published),
            view=ui.view_of(*controls(), *link_buttons),
        )
    except discord.HTTPException:
        # The change is saved; do not lose the confirmation over a preview Discord refuses.
        await _show(interaction, f"{headline}\n{PREVIEW_REFUSED}", view=ui.view_of(*controls()))


async def _save_failed(
    interaction: discord.Interaction,
    exc: ServiceError | ui.UserError,
    sent: Sequence[tuple[str, str]],
    *controls: discord.ui.Item[Any],
) -> None:
    """Say why a form was not saved, with what was typed in it so that it is not lost."""
    ui.refused(interaction, str(exc))
    sent = [(label, value.replace("```", "`​`​`")) for label, value in sent if value]
    head = f"{exc}\nNothing was saved."
    whole = " This is what you sent, so that you can copy it:"
    partly = " What you sent did not fit here completely, so only the beginning is shown:"
    frames = [f"\n**{label}**\n```\n" for label, _ in sent]
    close = "\n```"
    room = ui.MESSAGE_LIMIT - len(head) - sum(len(frame) + len(close) for frame in frames)
    fits = sum(len(value) for _, value in sent) <= room - len(whole)
    if sent:
        head += whole if fits else partly
        room -= len(whole if fits else partly)
    values = _share([value for _, value in sent], room)
    content = head + "".join(
        frame + value + close for frame, value in zip(frames, values, strict=True)
    )
    await _show(interaction, content, view=ui.view_of(*controls))


# -- Shared buttons --


class BackToFeed(ui.ActionButton, action="tpl_back", ids=1, requires=Level.MANAGER):
    label = "Back to Feed"

    async def handle(self, interaction: discord.Interaction) -> None:
        from .feed import open_panel

        feed = ui.feed_of(interaction, self.ids[0])
        await open_panel(interaction, feed.id, edit=True)


# -- Message text --


def _text_controls(feed_id: int, *, offer_embed: bool = False) -> Controls:
    return lambda: [
        EditText(feed_id, label="Edit again"),
        *([EditEmbed(feed_id, KEEP_COLOUR, label="Add Embed")] if offer_embed else []),
        BackToFeed(feed_id),
    ]


async def open_text(interaction: discord.Interaction, feed_id: int) -> None:
    """Open the form for the Feed's message text. Must be the first response."""
    ui.require_manager(interaction)
    feed = ui.feed_of(interaction, feed_id)
    await ui.show_form(
        interaction,
        "tpl_text",
        feed.id,
        title="Message text",
        fields=[
            ui.text_field(
                "text",
                "Message text",
                default=feed.text_template,
                required=False,
                long=True,
                max_length=MAX_CONTENT,
                description=TEXT_HINT,
            )
        ],
    )


class EditText(ui.ActionButton, action="tpl_text", ids=1, requires=Level.MANAGER):
    label = "Message text"

    async def handle(self, interaction: discord.Interaction) -> None:
        await open_text(interaction, self.ids[0])


@ui.form_handler("tpl_text", ids=1, requires=Level.MANAGER)
async def text_submitted(
    interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues
) -> None:
    feed = ui.feed_of(interaction, ids[0])
    text = values.text("text")
    try:
        await _service(interaction).set_text(
            feed.server_id, feed.id, text, actor=ui.actor_of(interaction)
        )
    except ServiceError as exc:
        await _save_failed(interaction, exc, [("Message text", text)], *_text_controls(feed.id)())
        return
    await _defer(interaction)
    if text:
        headline = f"Saved the message text of {_name(feed)}."
    elif feed.embed is not None:
        headline = f"{_name(feed)} has no message text now: only its Embed is posted."
    else:
        headline = (
            f"{_name(feed)} has no message text now. "
            "Until it has an Embed, the default message text is posted."
        )
    controls = _text_controls(feed.id, offer_embed=not text and feed.embed is None)
    await _result(interaction, feed.id, headline, controls)


# -- Embed --


def _embed_controls(interaction: discord.Interaction, feed_id: int) -> Controls:
    embed = ui.feed_of(interaction, feed_id).embed

    def controls() -> Sequence[discord.ui.Item[Any]]:
        if embed is None:
            return [EditEmbed(feed_id, KEEP_COLOUR, label="Edit again"), BackToFeed(feed_id)]
        return [
            ColourSelect(feed_id, current=embed.colour, chosen=True),
            EditEmbed(feed_id, KEEP_COLOUR, label="Edit again"),
            OpenFields(feed_id),
            ToggleTimestamp(
                feed_id, 0 if embed.timestamp else 1, label=_timestamp_label(embed.timestamp)
            ),
            RemoveEmbed(feed_id),
            BackToFeed(feed_id),
        ]

    return controls


async def open_embed(
    interaction: discord.Interaction, feed_id: int, *, colour_code: int = KEEP_COLOUR
) -> None:
    """Open the form for the Feed's Embed. Must be the first response."""
    ui.require_manager(interaction)
    feed = ui.feed_of(interaction, feed_id)
    embed = feed.embed or EmbedSpec()
    await ui.show_form(
        interaction,
        "tpl_embed",
        feed.id,
        colour_code,
        title="Embed",
        fields=[
            ui.text_field(
                "title",
                "Title",
                default=embed.title,
                required=False,
                max_length=MAX_EMBED_TITLE,
                description=HINT,
            ),
            ui.text_field(
                "description",
                "Description",
                default=embed.description,
                required=False,
                long=True,
                description=HINT,
            ),
            ui.text_field(
                "url",
                "Link",
                default=embed.url,
                required=False,
                max_length=MAX_EMBED_URL,
                description="Where the title leads, such as {{link}}.",
            ),
            ui.text_field(
                "image",
                "Image",
                default=embed.image,
                required=False,
                max_length=MAX_EMBED_URL,
                description="The address of a picture, such as {{image}}.",
            ),
            ui.text_field(
                "footer",
                "Footer",
                default=embed.footer,
                required=False,
                max_length=MAX_EMBED_FOOTER,
                description=HINT,
            ),
        ],
    )


class EditEmbed(ui.ActionButton, action="tpl_embed", ids=2, requires=Level.MANAGER):
    """Its second id is the colour to apply with the form, as _colour_code() gives it."""

    label = "Embed"

    async def handle(self, interaction: discord.Interaction) -> None:
        await open_embed(interaction, self.ids[0], colour_code=self.ids[1])


@ui.form_handler("tpl_embed", ids=2, requires=Level.MANAGER)
async def embed_submitted(
    interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues
) -> None:
    feed = ui.feed_of(interaction, ids[0])
    parts = {key: values.text(key) for key in ("title", "description", "url", "image", "footer")}
    service = _service(interaction)
    has_fields = feed.embed is not None and bool(feed.embed.fields)
    try:
        if not any(parts.values()) and not has_fields:
            await service.remove_embed(feed.server_id, feed.id, actor=ui.actor_of(interaction))
            headline = f"{_name(feed)} has no Embed now: every box was left empty."
        else:
            _check_address("The Embed link", parts["url"])
            _check_address("The Embed image", parts["image"])
            saved = await service.set_embed(
                feed.server_id,
                feed.id,
                **parts,
                colour=_colour_of(ids[1]),
                actor=ui.actor_of(interaction),
            )
            if saved.embed is None:  # a link alone: the service keeps no Embed that shows nothing
                headline = f"{_name(feed)} has no Embed now: a link alone shows nothing."
            else:
                headline = f"Saved the Embed of {_name(feed)}."
    except (ServiceError, ui.UserError) as exc:
        sent = [
            ("Title", parts["title"]),
            ("Description", parts["description"]),
            ("Link", parts["url"]),
            ("Image", parts["image"]),
            ("Footer", parts["footer"]),
        ]
        await _save_failed(
            interaction,
            exc,
            sent,
            EditEmbed(feed.id, ids[1], label="Edit again"),
            BackToFeed(feed.id),
        )
        return
    await _defer(interaction)
    await _result(interaction, feed.id, headline, _embed_controls(interaction, feed.id))


class ColourSelect(ui.ActionSelect, action="tpl_colour", ids=1, requires=Level.MANAGER):
    def build(
        self, custom_id: str, *, current: int | None = None, chosen: bool = False
    ) -> discord.ui.Select[discord.ui.View]:
        """`chosen` marks the option for `current` (None: no colour) as the one picked."""
        named = {value for _, value in COLOURS}
        placeholder = "Choose a colour"
        if chosen and current is not None and current not in named:
            placeholder = f"Colour: #{current:06x}"
        options = [
            discord.SelectOption(
                label=name, value=f"{value:06x}", default=chosen and value == current
            )
            for name, value in COLOURS
        ]
        options.append(
            discord.SelectOption(
                label="No colour", value=NO_COLOUR_VALUE, default=chosen and current is None
            )
        )
        return discord.ui.Select(custom_id=custom_id, placeholder=placeholder, options=options)

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        if feed.embed is None:
            raise ui.UserError("This Feed has no Embed. Use /template embed to make one.")
        picked = next(iter(self.picked), "")
        names = {f"{value:06x}": name for name, value in COLOURS}
        if picked == NO_COLOUR_VALUE:
            colour: int | str = ""
            headline = f"Removed the colour of the Embed of {_name(feed)}."
        elif picked in names:
            colour = int(picked, 16)
            headline = f"Set the colour of the Embed of {_name(feed)} to {names[picked]}."
        else:
            raise ui.UserError("Choose a colour from the list.")
        await ui.defer(interaction, update=True)
        await _service(interaction).set_embed(
            feed.server_id, feed.id, colour=colour, actor=ui.actor_of(interaction)
        )
        await _result(interaction, feed.id, headline, _embed_controls(interaction, feed.id))


def _timestamp_label(on: bool) -> str:
    return f"Append post date to footer (local time): {'on' if on else 'off'}"


class ToggleTimestamp(ui.ActionButton, action="tpl_embed_time", ids=2, requires=Level.MANAGER):
    """Its second id is what a click sets: 1 shows the post date, 0 does not.

    Carried in the id so that a click on a stale message sets the state it shows
    instead of flipping whatever is saved now.
    """

    label = _timestamp_label(True)

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        if feed.embed is None:
            raise ui.UserError("This Feed has no Embed.")
        on = self.ids[1] == 1
        await ui.defer(interaction, update=True)
        await _service(interaction).set_embed(
            feed.server_id, feed.id, timestamp=on, actor=ui.actor_of(interaction)
        )
        state = "now shows" if on else "no longer shows"
        headline = f"The Embed of {_name(feed)} {state} the post date after the footer."
        await _result(interaction, feed.id, headline, _embed_controls(interaction, feed.id))


class RemoveEmbed(ui.ActionButton, action="tpl_embed_remove", ids=1, requires=Level.MANAGER):
    label = "Remove Embed"
    style = discord.ButtonStyle.danger

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        if feed.embed is None:
            raise ui.UserError("This Feed has no Embed.")
        count = len(feed.embed.fields)
        fields = " with its Field" if count == 1 else f" with its {count} Fields"
        # Cancel leads back to the Embed screen this confirmation replaces.
        view = ui.view_of(RemoveEmbedConfirmed(feed.id), KeepEmbed(feed.id))
        await ui.edit(
            interaction, f"Remove the Embed of {_name(feed)}{fields if count else ''}?", view=view
        )


class KeepEmbed(ui.ActionButton, action="tpl_embed_keep", ids=1, requires=Level.MANAGER):
    label = "Cancel"

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        if feed.embed is None:  # removed meanwhile, from another message
            headline = f"{_name(feed)} has no Embed."
        else:
            headline = f"Kept the Embed of {_name(feed)}."
        await ui.defer(interaction, update=True)
        await _result(interaction, feed.id, headline, _embed_controls(interaction, feed.id))


class RemoveEmbedConfirmed(
    ui.ActionButton, action="tpl_embed_remove_yes", ids=1, requires=Level.MANAGER
):
    label = "Remove Embed"
    style = discord.ButtonStyle.danger

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        await _service(interaction).remove_embed(
            feed.server_id, feed.id, actor=ui.actor_of(interaction)
        )
        await ui.edit(
            interaction,
            f"Removed the Embed of {_name(feed)}.",
            view=ui.view_of(
                EditEmbed(feed.id, KEEP_COLOUR, label="Add Embed"), BackToFeed(feed.id)
            ),
        )


# -- Fields --


def _listing(head: Sequence[str], rows: Callable[[int], Sequence[str]], tail: Sequence[str]) -> str:
    """The list message, showing as much of each row as fits."""
    content = ""
    for width in (40, 24, 12):
        content = "\n".join([*head, *rows(width), *tail])
        if len(content) <= ui.MESSAGE_LIMIT:
            break
    return content


def _fields_panel(feed: Feed, note: str | None = None) -> tuple[str, discord.ui.View]:
    """The list of the Embed's Fields, rebuilt from the Feed each time it is shown."""
    fields: tuple[FieldSpec, ...] = () if feed.embed is None else feed.embed.fields
    full = len(fields) >= MAX_EMBED_FIELDS
    head = [note] if note else []
    head.append(f"**Fields of the Embed of {_name(feed)}** ({len(fields)} of {MAX_EMBED_FIELDS})")
    if not fields:
        creates = " Adding one makes the Embed." if feed.embed is None else ""
        head.append(f"There are no Fields yet.{creates}")
    tail = []
    if full:
        tail.append(
            f"The limit of {MAX_EMBED_FIELDS} Fields is reached. Remove one to add another."
        )

    def rows(width: int) -> list[str]:
        return [
            f"{number}. {_one_line(field.name, width)}: {_one_line(field.value, width)}"
            + (" (side by side)" if field.inline else "")
            for number, field in enumerate(fields, start=1)
        ]

    view = ui.view_of(
        AddField(feed.id, disabled=full),
        EditEmbed(feed.id, KEEP_COLOUR, label="Edit Embed"),
        BackToFeed(feed.id),
        RemoveField(feed.id, fields=fields) if fields else None,
    )
    return _listing(head, rows, tail), view


async def open_fields(interaction: discord.Interaction, feed_id: int) -> None:
    """Show the Embed's Fields with the controls to add and remove them."""
    ui.require_manager(interaction)
    content, view = _fields_panel(ui.feed_of(interaction, feed_id))
    await _show(interaction, content, view=view)


class OpenFields(ui.ActionButton, action="tpl_fields", ids=1, requires=Level.MANAGER):
    label = "Fields"

    async def handle(self, interaction: discord.Interaction) -> None:
        await open_fields(interaction, self.ids[0])


class AddField(ui.ActionButton, action="tpl_field_add", ids=1, requires=Level.MANAGER):
    label = "Add Field"
    style = discord.ButtonStyle.primary

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        if feed.embed is not None and len(feed.embed.fields) >= MAX_EMBED_FIELDS:
            raise ui.UserError(f"An Embed can have at most {MAX_EMBED_FIELDS} Fields.")
        await ui.show_form(
            interaction,
            "tpl_field",
            feed.id,
            title="Add a Field",
            fields=[
                ui.text_field("name", "Name", max_length=MAX_FIELD_NAME, description=HINT),
                ui.text_field(
                    "value", "Value", long=True, max_length=MAX_FIELD_VALUE, description=HINT
                ),
                ui.choice_field(
                    "inline",
                    "Side by side",
                    [("no", "No"), ("yes", "Yes")],
                    default="no",
                    description="Whether the Field may sit next to other side by side Fields.",
                ),
            ],
        )


@ui.form_handler("tpl_field", ids=1, requires=Level.MANAGER)
async def field_submitted(
    interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues
) -> None:
    feed = ui.feed_of(interaction, ids[0])
    name, value = values.text("name"), values.text("value")
    inline = values.choice("inline") == "yes"
    try:
        feed = await _service(interaction).add_field(
            feed.server_id, feed.id, name, value, inline, actor=ui.actor_of(interaction)
        )
    except ServiceError as exc:
        await _save_failed(
            interaction,
            exc,
            [("Name", name), ("Value", value)],
            AddField(feed.id, label="Try again"),
            OpenFields(feed.id, label="Back to Fields"),
        )
        return
    content, view = _fields_panel(feed, f"Added Field {len(feed.embed.fields)}.")
    await _show(interaction, content, view=view)


class RemoveField(ui.ActionSelect, action="tpl_field_remove", ids=1, requires=Level.MANAGER):
    def build(
        self, custom_id: str, *, fields: Sequence[FieldSpec] = ()
    ) -> discord.ui.Select[discord.ui.View]:
        options = [
            discord.SelectOption(
                label=ui.cut(f"{number}. {' '.join(field.name.split())}", 100),
                value=f"{number}:{_mark(field.name, field.value)}",
                description=ui.cut(" ".join(field.value.split()), 100) or None,
            )
            for number, field in enumerate(fields[: ui.CHOICE_LIMIT], start=1)
        ]
        return discord.ui.Select(custom_id=custom_id, placeholder="Remove a Field", options=options)

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        picked = _picked_entry(self.picked)
        if picked is None:
            raise ui.UserError("Choose the Field to remove.")
        position, mark = picked
        fields = () if feed.embed is None else feed.embed.fields
        # The list may have changed since the panel was shown: remove only what was picked.
        if 1 <= position <= len(fields) and mark == _mark(
            fields[position - 1].name, fields[position - 1].value
        ):
            feed = await _service(interaction).remove_field(
                feed.server_id, feed.id, position, actor=ui.actor_of(interaction)
            )
            content, view = _fields_panel(feed, f"Removed Field {position}.")
        else:
            content, view = _fields_panel(feed, LIST_CHANGED)
        await ui.edit(interaction, content, view=view)


# -- Buttons --


def _buttons_panel(feed: Feed, note: str | None = None) -> tuple[str, discord.ui.View]:
    """The list of the Feed's Buttons, rebuilt from the Feed each time it is shown."""
    buttons = feed.buttons
    full = len(buttons) >= MAX_BUTTONS
    head = [note] if note else []
    head.append(f"**Buttons of {_name(feed)}** ({len(buttons)} of {MAX_BUTTONS})")
    if not buttons:
        head.append("There are no Buttons yet. A Button is a link posted under the message.")
    tail = []
    if full:
        tail.append(f"The limit of {MAX_BUTTONS} Buttons is reached. Remove one to add another.")

    def rows(width: int) -> list[str]:
        return [
            f"{number}. {_one_line(button.label, width)} opens {_one_line(button.url, width * 2)}"
            for number, button in enumerate(buttons, start=1)
        ]

    view = ui.view_of(
        AddButton(feed.id, disabled=full),
        BackToFeed(feed.id),
        RemoveButton(feed.id, buttons=buttons) if buttons else None,
    )
    return _listing(head, rows, tail), view


async def open_buttons(interaction: discord.Interaction, feed_id: int) -> None:
    """Show the Feed's Buttons with the controls to add and remove them."""
    ui.require_manager(interaction)
    content, view = _buttons_panel(ui.feed_of(interaction, feed_id))
    await _show(interaction, content, view=view)


class OpenButtons(ui.ActionButton, action="tpl_buttons", ids=1, requires=Level.MANAGER):
    label = "Buttons"

    async def handle(self, interaction: discord.Interaction) -> None:
        await open_buttons(interaction, self.ids[0])


class AddButton(ui.ActionButton, action="tpl_button_add", ids=1, requires=Level.MANAGER):
    label = "Add Button"
    style = discord.ButtonStyle.primary

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        if len(feed.buttons) >= MAX_BUTTONS:
            raise ui.UserError(f"A Feed can have at most {MAX_BUTTONS} Buttons.")
        await ui.show_form(
            interaction,
            "tpl_button",
            feed.id,
            title="Add a Button",
            fields=[
                ui.text_field("label", "Label", max_length=MAX_BUTTON_LABEL, description=HINT),
                ui.text_field(
                    "url",
                    "Address",
                    default="{{link}}",
                    max_length=MAX_BUTTON_URL,
                    description="The web address it opens. {{link}} is the Item's address.",
                ),
            ],
        )


@ui.form_handler("tpl_button", ids=1, requires=Level.MANAGER)
async def button_submitted(
    interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues
) -> None:
    feed = ui.feed_of(interaction, ids[0])
    label, url = values.text("label"), values.text("url")
    try:
        _check_address("The Button address", url)
        feed = await _service(interaction).add_button(
            feed.server_id, feed.id, label, url, actor=ui.actor_of(interaction)
        )
    except (ServiceError, ui.UserError) as exc:
        await _save_failed(
            interaction,
            exc,
            [("Label", label), ("Address", url)],
            AddButton(feed.id, label="Try again"),
            OpenButtons(feed.id, label="Back to Buttons"),
        )
        return
    content, view = _buttons_panel(feed, f"Added Button {len(feed.buttons)}.")
    await _show(interaction, content, view=view)


class RemoveButton(ui.ActionSelect, action="tpl_button_remove", ids=1, requires=Level.MANAGER):
    def build(
        self, custom_id: str, *, buttons: Sequence[ButtonSpec] = ()
    ) -> discord.ui.Select[discord.ui.View]:
        options = [
            discord.SelectOption(
                label=ui.cut(f"{number}. {' '.join(button.label.split())}", 100),
                value=f"{number}:{_mark(button.label, button.url)}",
                description=ui.cut(button.url, 100) or None,
            )
            for number, button in enumerate(buttons[: ui.CHOICE_LIMIT], start=1)
        ]
        return discord.ui.Select(
            custom_id=custom_id, placeholder="Remove a Button", options=options
        )

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        picked = _picked_entry(self.picked)
        if picked is None:
            raise ui.UserError("Choose the Button to remove.")
        position, mark = picked
        buttons = feed.buttons
        # The list may have changed since the panel was shown: remove only what was picked.
        if 1 <= position <= len(buttons) and mark == _mark(
            buttons[position - 1].label, buttons[position - 1].url
        ):
            feed = await _service(interaction).remove_button(
                feed.server_id, feed.id, position, actor=ui.actor_of(interaction)
            )
            content, view = _buttons_panel(feed, f"Removed Button {position}.")
        else:
            content, view = _buttons_panel(feed, LIST_CHANGED)
        await ui.edit(interaction, content, view=view)


# -- Reset --


class ResetTemplate(ui.ActionButton, action="tpl_reset", ids=1, requires=Level.MANAGER):
    label = "Reset"
    style = discord.ButtonStyle.danger

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        await _service(interaction).reset_template(
            feed.server_id, feed.id, actor=ui.actor_of(interaction)
        )
        await ui.edit(
            interaction,
            f"Reset the Template of {_name(feed)}: "
            "the default message text, no Embed and no Buttons.",
            view=ui.view_of(BackToFeed(feed.id)),
        )


# -- Commands --

group = app_commands.guild_only()(
    app_commands.Group(name="template", description="Change what a Feed posts for each Item.")
)


@group.command(name="text", description="Edit a Feed's message text.")
@app_commands.describe(feed="The Feed to edit.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def text_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    await open_text(interaction, ui.feed_from_option(interaction, feed).id)


@group.command(name="embed", description="Edit the Embed posted under a Feed's message text.")
@app_commands.describe(
    feed="The Feed to edit.",
    colour="A hex code such as #ff8800, or none for no colour. Leave out to keep the colour.",
)
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def embed_command(
    interaction: discord.Interaction, feed: str, colour: str | None = None
) -> None:
    ui.require_manager(interaction)
    found = ui.feed_from_option(interaction, feed)
    await open_embed(interaction, found.id, colour_code=_colour_code(colour))


@group.command(name="fields", description="Add or remove the Fields of a Feed's Embed.")
@app_commands.describe(feed="The Feed to edit.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def fields_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    await open_fields(interaction, ui.feed_from_option(interaction, feed).id)


@group.command(name="buttons", description="Add or remove the link Buttons under a Feed's message.")
@app_commands.describe(feed="The Feed to edit.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def buttons_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    await open_buttons(interaction, ui.feed_from_option(interaction, feed).id)


@group.command(name="reset", description="Put a Feed's Template back to the default.")
@app_commands.describe(feed="The Feed to reset.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def reset_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    found = ui.feed_from_option(interaction, feed)
    await ui.reply(
        interaction,
        f"Reset the Template of {_name(found)}? Its message text goes back to the default, "
        "and its Embed, Fields and Buttons are removed.",
        view=ui.confirm_view(ResetTemplate(found.id)),
    )


@group.command(
    name="placeholders", description="List every Placeholder with its value for the newest Item."
)
@app_commands.describe(feed="The Feed whose newest Item supplies the values.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def placeholders_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    found = ui.feed_from_option(interaction, feed)
    await ui.defer(interaction)
    pairs = await _service(interaction).placeholder_values(found.server_id, found.id)
    head = [f"**Placeholders** with their values for the newest Item of {_name(found)}:"]
    if not any(value.strip() for _, value in pairs):
        head.append(NO_ITEM_VALUES)
    names = [f"`{{{{{name}}}}}` " for name, _ in pairs]
    shown = [" ".join(value.split()) or "(empty)" for _, value in pairs]
    fixed = "\n".join([*head, *names, *PLACEHOLDER_NOTES])
    shown = _share(shown, ui.MESSAGE_LIMIT - len(fixed))
    rows = [name + value for name, value in zip(names, shown, strict=True)]
    await ui.reply(interaction, "\n".join([*head, *rows, *PLACEHOLDER_NOTES]))


def register(tree: app_commands.CommandTree) -> None:
    tree.add_command(group)
