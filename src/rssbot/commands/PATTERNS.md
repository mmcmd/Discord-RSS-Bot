# Command patterns

Copy `setup.py`. Import the toolkit as `from . import _ui as ui`. Tests use `tests/fakes_discord.py`
(`FakeInteraction`), see `tests/test_cmd_setup.py`. Wording follows `CONTEXT.md`.

## Rules

- Every reply goes through `ui.reply` / `ui.edit` (private, no pings). Never call `interaction.response.send_message` yourself.
- Access is checked in the bot every time: first line of a command is `ui.require_manager(interaction)` or `ui.require_admin(interaction)`; buttons, selects and forms state `requires=` and the toolkit checks it. Do not use `default_permissions`. The one exception is `/help`, open to anyone in a Server (`ui.require(interaction, None)`): it shows each member only the categories of their Level.
- Every Feed id received (option, custom id) goes through `ui.feed_of(interaction, feed_id)`: it raises unless the Feed is in this Server.
- Expected failure: `raise ui.UserError("One plain sentence.")`. Exceptions named `ServiceError`/`TemplateError` are shown the same way (`user_message` attribute, else `str(exc)`). `FeedNotFound`, `discord.Forbidden` and `discord.NotFound` each get a fixed sentence. Anything else is logged and shown as "Something went wrong. It has been logged." Do not catch exceptions to reply yourself.
- "That Feed no longer exists." raised from a click replaces the clicked message and removes its controls; do not raise it for a message that should stay (a list).
- No state in the process: no `View` subclass, no timeout, no dict of open panels. Rebuild the message from the database on every click.
- Shared objects: `ui.deps(interaction).db` (also `.service`, `.deliverer`, `.scheduler`, `.journal`).
- Who did it: every `service` method that changes something takes `actor=ui.actor_of(interaction)`; the service saves the Log entry and starts its report. Do not record it again.
- A change made straight through `db` (a Grant, the Logs channel) is recorded by the command, after the change: `ui.record(interaction, LogKind.GRANT_GIVEN, detail=..., changes=[Change("Level", "", "Admin")])`. It goes through the journal and never raises for a failed save. Nothing is recorded for a change that changes nothing, a refusal, a preview or an export.
- Showing who made a Log entry: `shown = await ui.actors_of(interaction, entries)` (only the entries on screen), then `ui.actor_words(entry, shown)`. A member who left reads `Name (left the Server)`; the lookups defer the interaction, so call it before the first reply. `commands/_history.py` renders Log entries as lines.
- The container log gets one line per command, click and form from the toolkit (`rssbot.commands`, `outcome=ok|refused|error`); do not log them yourself. A handler that turns the member down without raising calls `ui.refused(interaction, reason)`. A `UserError` whose sentence repeats what was typed (a Feed address) takes `log_reason=` without it.
- `from __future__ import annotations` works in command modules (verified: discord.py resolves the strings against the module's globals), so everything used in a command's annotations must be imported at module level, not under `TYPE_CHECKING`.

## A module

```python
@app_commands.command(name="feeds", description="List this Server's Feeds.")  # description <= 100
@app_commands.guild_only()
@app_commands.describe(feed="The Feed to show.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def feeds_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    found = ui.feed_from_option(interaction, feed)   # option value is the Feed id as a string
    await ui.reply(interaction, f"{found.name} in {ui.channel_mention(found.channel_id)}")

def register(tree: app_commands.CommandTree) -> None:    # the convention: one per module
    tree.add_command(feeds_command)
```

Then add the module to `MODULES` in `commands/__init__.py`. Importing it registers its buttons, selects and forms.
Then write the command's `Entry` in `commands/help.py`: `/help` is written by hand. Do the same when a command is renamed or removed, or its options change. `tests/test_cmd_help.py` fails until the entries and their options match the registered commands.
Subcommands: `group = app_commands.guild_only()(app_commands.Group(name=..., description=...))`, `@group.command(...)`, `tree.add_command(group)`.
(Verified: the decorator on the group yields `contexts: [0]`; `Group(guild_only=True)` alone does not, and `guild_only` on a subcommand is ignored.)

## Buttons and selects (stateless)

Custom id is `rss:c:<action>[:<id>...]`, built and parsed by the base class. `action` is unique, `[a-z][a-z0-9_]*`;
`ids=` is how many non-negative integers it carries. `requires=` is mandatory: `Level.MANAGER`, `Level.ADMIN` or `None` (anyone).

```python
class PauseFeed(ui.ActionButton, action="feed_pause", ids=1, requires=Level.MANAGER):
    label = "Pause"                                  # style = discord.ButtonStyle.secondary by default
    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        ...
        await ui.edit(interaction, content, view=view)   # replaces the message; omitted parts are removed

class IntervalSelect(ui.ActionSelect, action="feed_interval", ids=1, requires=Level.MANAGER):
    def build(self, custom_id: str, *, current: str | None = None) -> discord.ui.Select:
        return discord.ui.Select(custom_id=custom_id, options=[...])   # must work with no extra arguments
    async def handle(self, interaction): self.picked        # list[str]; self.picked_ids for channel/role selects

view = ui.view_of(PauseFeed(feed.id), IntervalSelect(feed.id, current="600"), PauseFeed(feed.id, label="x", disabled=True))
```

Per-instance look: `label=`, `style=`, `disabled=`, `emoji=`, `row=` for buttons; your own `build` keywords for selects.
Two items in one message must not share a custom id. A message holds 5 rows; a select fills a row, a row holds 5 buttons.

- Confirm: `await ui.reply(interaction, "Remove this Feed?", view=ui.confirm_view(RemoveFeed(feed.id, label="Remove", style=discord.ButtonStyle.danger)))`. `RemoveFeed.handle` does the removal; Cancel is built in.
- Pages: `class FeedsPage(ui.PageButton, action="feeds_page", ids=1, requires=Level.MANAGER)`; the last id is the page (`self.page`).
  `page = ui.paginate(items, n)` gives `.items`, `.page`, `.pages`, `.footer`; `ui.view_of(*ui.page_buttons(FeedsPage, page=page.page, pages=page.pages))`. `labels=` and `row=` change the two buttons' text and row.
- A click or form still unanswered after `ui.ANSWER_WITHIN_S` is deferred for its handler (or told to try again if no handler ran) and logged as a warning, so "This interaction failed" always leaves a trace. Still defer yourself before slow work.
- Slow work (fetching a URL): `await ui.defer(interaction)` then `ui.reply` (private "thinking", then the answer), or in a component `await ui.defer(interaction, update=True)` then `ui.edit`.

## Pop-up forms (modals)

Stateless: the modal's custom id is `rss:m:<action>[:<id>...]`; submissions are dispatched to the handler from `ui.handle_interaction`
(hooked onto `on_interaction` by `register_dynamic_items`), with access re-checked. A form must be the FIRST response (never after `defer`),
so read what pre-fills it from SQLite first. `ui.edit` in a handler replaces the message if the form was opened from a button; opened from a slash command it replies instead.

```python
@ui.form_handler("feed_edit", ids=1, requires=Level.MANAGER)
async def feed_edit_submitted(interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues) -> None:
    feed = ui.feed_of(interaction, ids[0])
    name = values.text("name")                 # stripped; "" if left empty
    channel = values.channel("channel")        # PickedChannel(id, type, name) | None
    interval = values.choice("interval")       # str | None;  values.ids("roles") -> list[int]
    await ui.defer(interaction)                # then slow work
    await ui.reply(interaction, "Saved.")

await ui.show_form(interaction, "feed_edit", feed.id, title="Edit Feed", fields=[     # 1 to 5 fields
    ui.text_field("name", "Name", default=feed.name, max_length=80),
    ui.text_field("text", "Message text", default=feed.text_template, long=True, required=False),
    ui.channel_field("channel", "Channel", channel_types=[discord.ChannelType.text], default_id=feed.channel_id),
    ui.choice_field("interval", "Check every", [("600", "10 minutes"), ("3600", "1 hour")], default="600"),
    ui.role_field("roles", "Roles to mention", default_ids=feed.mention_role_ids, max_values=5),
])
```

## Verified in the installed discord.py 2.7.1 source

Modals (`ui/modal.py`, `ui/label.py`, `ui/select.py`, `ui/text_input.py`, `state.py`):
- At most 5 top-level components (`Modal.add_item` raises `ValueError` on the 6th). Title, label text <= 45 characters; label description <= 100; text default <= 4000.
- `ui.Label(text=, description=, component=)` wraps exactly one component and serialises as type 18. Accepted inside: `TextInput`, `Select` (string), `ChannelSelect`, `RoleSelect`, `UserSelect`, `MentionableSelect`. `TextInput(label=...)` and bare inputs (auto-wrapped in an action row) are deprecated; use `Label`.
- Selects take `required=` in modals (default `True` for `Select`, `False` for the others). Pre-select with `default_values=[discord.Object(id)]` (channel, role) or `SelectOption(default=True)`.
- Submission payload: `data["components"]` is a list of `{type: 18, component: {custom_id, value | values}}` (or `{type: 1, components: [...]}`); picked channels and roles are described in `data["resolved"]`. `ui.FormValues` reads exactly this.
- The library dispatches a submission only to a `Modal` instance it stored under that exact custom id (`ViewStore._modals`); there is no template matching for modals, so after a restart a submission would be dropped. `send_modal` skips storing a modal that `is_finished()`, and the `interaction` event fires for every interaction. Hence our route: `ui.build_form` calls `modal.stop()`, and `ui.handle_interaction` parses the raw payload.

Dynamic items (`ui/dynamic.py`, `ui/view.py`):
- `client.add_dynamic_items(*classes)` once at start-up; the template is matched with `fullmatch`; EVERY matching class is dispatched, so templates must not overlap (tested).
- Exceptions in `from_custom_id` and `callback` are only logged, leaving "This interaction failed": `ui.Action` never raises from the first and wraps the second.
- A click needs `interaction.message`; the item is rebuilt from the message, so its look inside `handle` is whatever was sent.
- A `View(timeout=None)` holding only dynamic items is not kept by the library (no per-message entry). `ui.view_of` builds that.
- A component's `response.defer()` is a silent "update" defer; with `thinking=True` it posts a new message. A slash command's defer is always "thinking".
- `interaction.permissions` and the member's role ids come from the interaction payload; `guild.owner_id`, channels and roles come from the cache the `guilds` intent fills.

Not verifiable without a live connection: that Discord accepts each select type inside a modal for this bot, `min_values=0` on optional
modal selects, integer ids in `default_values`, and that `guild.me` carries the bot's roles (`ui.bot_can_post` relies on it).
