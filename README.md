# Discord RSS Bot

A self-hosted Discord bot that posts new items from RSS and Atom feeds into channels. You run it on your own machine with Docker; the people in your Discord servers add and manage feeds with slash commands.

What it is built to do well:

- **One failure never stops the rest.** An item that cannot be posted is retried a few times and then skipped. A feed that cannot be fetched is retried later, and the others carry on. Neither can hold up another item or another feed.
- **Messages you can change.** Every feed has its own message text, optional embed, link buttons, filters and role mentions, and can post under its own name and picture. Feeds can post into forum channels.
- **Small footprint.** The image is about 67 MB on disk (16 MB to download), runs as an unprivileged user, asks Discord for no privileged intents and keeps no message or member data. All state is one SQLite file.

**Not yet verified.** This version has been tested without a live Discord connection.

The words with capital letters in this file (Feed, Item, Check, Template and so on) are used in one fixed sense; `CONTEXT.md` defines them.

## Quick start

You need Docker with Compose, and a Discord account that can add bots to your server.

### 1. Create the bot in Discord

1. Open the [Discord developer portal](https://discord.com/developers/applications) and choose **New Application**. The application name is the bot's name.
2. Open the **Bot** page. Choose **Reset Token** and copy the token. Treat it like a password; anyone who has it controls the bot.
3. On the same page, leave all three **Privileged Gateway Intents** switched off. The bot needs none of them.
4. If only you will add the bot to servers, switch **Public Bot** off.

### 2. Invite it to your server

Open this address in a browser, with your own application ID (shown on the **General Information** page) in place of `YOUR_APPLICATION_ID`:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=309774699520
```

Or build the link yourself under **OAuth2 > URL Generator**: tick the scopes `bot` and `applications.commands`, and these permissions:

| Permission | What the bot uses it for |
| --- | --- |
| View Channels | Seeing the channels it posts in |
| Send Messages | Posting Items |
| Send Messages in Threads | Posting into threads |
| Create Public Threads | Creating a Forum post for each Item in a forum channel |
| Embed Links | Showing Embeds and link previews |
| Attach Files | Attaching a Cover image to a Forum post |
| Manage Webhooks | "Post as" a site or a custom name (see below) |
| Mention Everyone | Optional. Only needed to ping a role that is not set to "Allow anyone to @mention this role" |

The number in the link above is exactly this set. If you leave out Mention Everyone, use `permissions=309774568448`.

The bot also has to be able to see and post in the channels you point Feeds at. In a private channel, add the bot to it.

### 3. Run it

```
git clone https://github.com/mmcmd/Discord-RSS-Bot.git
cd Discord-RSS-Bot
cp .env.example .env
```

Open `.env` and put the token after `DISCORD_TOKEN=`. Then:

```
docker compose up -d
```

Until a release image has been published, the compose file has to build the bot from source. Use this instead:

```
docker compose up -d --build
```

The bot registers its slash commands when it starts. They can take a few minutes to show up in Discord the first time. To see what the bot is doing:

```
docker compose logs -f rssbot
```

To update later: `docker compose pull && docker compose up -d` (or `git pull && docker compose up -d --build` if you build from source).

## Configuration

All settings are environment variables, normally set in `.env`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | none, required | The bot token from the developer portal. Without it the container prints a message and exits with status 2. |
| `ALLOW_PRIVATE_URLS` | `false` | Allow Feed addresses (and the images and pages the bot reads for them) that lead to private, local or link-local network addresses. `1`, `true`, `yes` and `on` mean yes; anything else means no. Leave it off unless you want to follow feeds on your own network. |
| `LOG_LEVEL` | `INFO` | How much the bot writes to the container log: one of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. See "Reading the logs". Any other value stops the bot with status 2 and a message saying so. |
| `DATA_DIR` | `/data` | Where the database lives. The image sets this to the `/data` volume; do not set it in `.env`. If the bot cannot write to this folder it prints `Configuration problem: ...` saying so and exits with status 2. |

If Discord refuses the token, the bot logs that and exits with status 1.

## First steps in Discord

Replies to the commands below are private: only the person who used the command sees them.

1. **`/setup`** (Admins only). Choose the Logs channel: a text or announcement channel where the bot reports what members do to Feeds and access, and what goes wrong with a Feed (a Broken feed, a Paused feed). This is optional but worth doing, because it is the only place the bot tells you something is wrong. See "Log entries and the Logs channel".
2. **`/access grant`** (Admins only). The server owner and anyone with Discord's Administrator permission are Admins already, and Admins can do everything a Manager can. To let other people manage Feeds, grant a role or a member the **Manager** level. Grant **Admin** only to people who should also give and take away access.
3. **`/feed add`** (Managers). A form asks for the Feed address (a feed URL; `https://` is added if you leave it off), the channel, how often to check, and who to post as. The bot reads the feed once to make sure it works and then shows the Feed's panel. From the panel, press **Test** to see privately how the newest Item would look, and **Post to channel** to send it for real.

Everyone in the server can see the commands in Discord's list; the bot refuses people who have no access with "Only Managers and Admins of this Server can do that." The exception is `/help`, which anyone can use.

## Commands

A Feed is chosen by name in the `feed` option of a command: start typing and pick from the list that appears. It can match the Feed's name or its address. When two Feeds would look the same in that list, the site's host is added to tell them apart.

| Command | Who | What it does |
| --- | --- | --- |
| `/setup` | Admin | Shows a panel to choose the Logs channel (text or announcement channel) or clear it. Warns if the bot cannot post in the chosen channel. Changing it is itself saved as a Log entry. |
| `/access grant target level` | Admin | Gives a role or a member the Admin or Manager level. A second grant to the same target replaces the first. `@everyone` and bots are refused. |
| `/access revoke [target] [target_id]` | Admin | Takes a Grant away. Pick the role or member, or type the ID shown in `/access list` for a role that was deleted or a member who left. |
| `/access list` | Admin | Shows who has been given access, grouped by level. |
| `/feed add` | Manager | Opens the form described above and then the Feed's panel. Nothing is posted for Items the source lists at that moment. |
| `/feed list` | Manager | Lists the Server's Feeds with their channel, status (Working, Rate limited, Failing and why, or Paused and why) and when each was last checked, with a count of each status on top. A Feed that is not working also shows when it last worked and, unless it is paused, when its next Check is. A page holds as many Feeds as fit in one message, 25 at most. A menu opens any Feed's panel. A Paused feed's status says who paused it: `Paused by @member`, or `Paused by the bot:` and the reason, with when. |
| `/feed history feed` | Manager | Shows the Feed's Log entries, newest first, 10 to a page: when, who, what, and what changed. See "Log entries and the Logs channel". |
| `/log [member] [kind]` | Admin | Shows the Log entries of the whole Server, newest first, including those of Feeds that were removed and of Grants. `member` shows only what that member did; `kind` is one of **Feeds added and removed**, **Pauses and resumes**, **Edits**, **Access** or **The bot's own reports**. 10 to a page. |
| `/feed edit feed` | Manager | Opens the Feed's panel, from which everything about the Feed can be changed. |
| `/feed remove feed` | Manager | Removes a Feed with its Template and Filters, after asking you to confirm. |
| `/feed pause feed` | Manager | Stops checking the Feed until it is resumed. |
| `/feed resume feed` | Manager | Checks a Paused feed again, starting now. |
| `/feed refresh [feed]` | Manager | Checks the Feed now instead of at its next turn, posts any new Items and says how the Check went. Without `feed`, every Feed of the Server is refreshed, usually within a minute, except Paused feeds and Rate-limited feeds, whose site asked the bot to wait. |
| `/feed test feed` | Manager | Shows privately what the Feed would post for its newest Item, with a **Post to channel** button. Nothing is posted until you press it. |
| `/feed import file [channel]` | Manager | Adds a Feed for every address in an OPML file. See "OPML import and export". |
| `/feed export` | Manager | Sends the Server's Feeds as an OPML file (`feeds.opml`). |
| `/template text feed` | Manager | Edits the Feed's message text. |
| `/template embed feed [colour]` | Manager | Edits the Embed's title, description, link, image and footer. `colour` is a hex code such as `#ff8800`, or `none`; leave it out to keep the colour. |
| `/template fields feed` | Manager | Adds and removes the Embed's Fields. |
| `/template buttons feed` | Manager | Adds and removes the link Buttons. |
| `/template reset feed` | Manager | Puts the Template back to the default (after confirming): default message text, no Embed, no Buttons. |
| `/template placeholders feed` | Manager | Lists every Placeholder with its value for the Feed's newest Item, so you can see what each one gives. |
| `/filter feed` | Manager | Shows the Feed's Filters and lets you add and remove words. |
| `/help` | Anyone | Explains the commands, a category at a time: **Feeds** and **Templates** for Managers, plus **Admin** for Admins. Buttons switch category and ◀ ▶ turn the page. Someone with no access is told to ask an Admin for it. |

### The Feed panel

`/feed add`, `/feed edit` and the menu in `/feed list` open a panel for one Feed. Everything about a Feed can be changed from it:

| Button | What it opens |
| --- | --- |
| Settings | A form for the Feed's name, address, channel and check interval. Changing the address starts the Feed afresh from the new source: what it lists then counts as already seen. Moving it to another channel clears its forum tags. |
| Message text, Embed, Fields, Buttons | The Template forms described below |
| Filters | The Filter list |
| Mentions | A menu of up to 10 roles to mention with every Item (see "Role mentions") |
| Post as | A menu: The bot, The site's name and icon, A custom name and picture |
| Forum options | Only for Feeds in a forum channel: Post title, tags, Cover image |
| Test | The same as `/feed test` |
| Refresh | The same as `/feed refresh` for this Feed. Not shown on a Paused feed. |
| Pause or Resume | Stops or restarts Checks. A Paused feed's panel shows `Paused by @member`, and every panel shows `Added by @member`. |
| Remove | Removes the Feed, after asking you to confirm |

The check interval can be 5, 10, 15 or 30 minutes, or 1, 3, 6, 12 or 24 hours. The default is 10 minutes.

A Feed can post in a text channel, an announcement channel, a thread or a forum channel. The same address can be used in several channels, but only once in each; each of those is its own Feed.

## Templates

A Feed's Template says what is posted for each Item: message text, an Embed, or both, and up to five Buttons. Text in a Template is ordinary Discord text, so `**bold**`, `[links](https://example.com)` and emoji work. Placeholders in it are replaced with parts of the Item.

### Placeholder syntax

| Written as | Gives |
| --- | --- |
| `{{title}}` | The Item's title |
| `{{summary\|\|description}}` | A Fallback: the first of the names that is not empty. You can chain more than two, as in `{{summary\|\|content\|\|title}}`. |
| `{{description:200}}` | The description cut to at most 200 characters, ending in `…` if it was cut |
| `{{summary\|\|content:300}}` | Both together: the first one that is not empty, cut to 300 |

Names are not case-sensitive. A Placeholder that has no value, or only blanks, gives nothing. `{{url}}` is another name for `{{link}}`. A Placeholder name that does not exist is refused when you save the form, with a message saying which one. Everything outside `{{ }}` is posted as written.

The Placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{{title}}` | The Item's title, as plain text |
| `{{link}}` | The Item's web address |
| `{{description}}` | The summary if there is one, otherwise the full content |
| `{{summary}}` | The short text the source gives |
| `{{content}}` | The full text the source gives |
| `{{author}}` | The author's name |
| `{{date}}` | When the Item was published, as a Discord timestamp that shows in each reader's own time zone. Where Discord shows no timestamps (an Embed's title and footer, Field names, Buttons, Forum post titles) it is written out instead, as in `8 Oct 2026 14:05 UTC`. Empty if the source gives no date. |
| `{{categories}}` | The Item's categories, separated by commas |
| `{{image}}` | The address of the Item's main picture. Empty if there is none. |
| `{{feed_title}}` | The source's own title (the Feed's name if the source has none) |
| `{{feed_link}}` | The source's home page |
| `{{mentions}}` | The roles chosen under **Mentions**, as pings |

HTML in `summary` and `content` is turned into Discord formatting. Use `/template placeholders` to see the real values for a Feed.

### Message text

The default message text is:

```
📰 | **{{title}}**
{{link}}
```

Characters in an Item's title that Discord would read as formatting, such as `*`, `_` and `#`, are shown as written. A long title is shortened so that the link is never cut off. If an Item has no title, only the link is posted. Save the message text empty to post only the Embed; a Feed with neither message text nor an Embed posts the default message text. A message that would be over Discord's limit of 2000 characters is shortened in its longest Placeholder value, not at its end, so a link or `{{mentions}}` placed after a long description is kept. Example:

```
**{{title}}** by {{author}}
{{description:300}}
{{link}}
```

### Embeds

An Embed is the boxed card under the message text, or on its own if the message text is empty. Its parts are a title, a description, a link (where the title leads), an image, a footer and a colour; each of the text parts can use Placeholders, and the link and image must start with `http://`, `https://` or one of `{{link}}`, `{{image}}` and `{{feed_link}}`. For example, image `{{image}}` shows the Item's picture. Press **Embed** on the panel or use `/template embed`. If you save the form with every box empty (and no Fields), the Embed is removed. An Embed that has nothing in it for a particular Item is left out of that Item's message.

The colour is chosen from a menu after you save (red, orange, yellow, green, teal, blue, purple, pink, grey, dark, or no colour), or typed as a hex code in the `colour` option of `/template embed`.

Limits, enforced when posting by cutting the text: title 256 characters, description 4096, footer 2048; the whole Embed 6000 (the description is shortened first, then the last Fields are dropped).

### Fields

A Field is a labelled name and value box inside an Embed: a name (up to 256 characters), a value (up to 1024), both with Placeholders, and a choice whether it may sit **side by side** with other Fields. An Embed can have up to 25. Adding a Field to a Feed without an Embed creates the Embed. A Field whose name or value comes out empty for an Item is left out for that Item.

```
Name:  Author
Value: {{author||feed_title}}
```

### Buttons

A Button is a link button under the message: a label (up to 80 characters) and an address (up to 512), both with Placeholders. The form offers `{{link}}` as the address. An address must start with `http://`, `https://` or one of `{{link}}`, `{{image}}` and `{{feed_link}}`; a Placeholder further on in an address, as in `https://www.google.com/search?q={{title}}`, is percent-encoded so that spaces and other characters cannot break it (the same goes for an Embed's link and image). A Feed can have up to 5. A Button whose label or address comes out empty for an Item is left out for that Item.

## Filters

Filters decide which Items are posted. There are two lists:

- **Must-have words.** If the list has any words, an Item is posted only if it contains at least one of them.
- **Block words.** An Item that contains any of them is not posted.

Matching is on whole words and ignores capital letters, so `cat` matches "Cat" but not "category". Chinese, Japanese and Thai are written without spaces, so a word in one of them matches anywhere in the text: `東京` matches "今日は東京で会議". Accents and word endings must match exactly: `cafe` does not match "Café", and `bank` does not match "Banks". Only the text a reader sees is searched, not the addresses of the links in it. A phrase of several words, such as `breaking news`, must appear in that order. Each word can be limited to look in: title and description (the default), title, description, category or author.

Add words with **Add must-have words** or **Add block words**: one word or phrase per line, up to 100 characters each, and up to 100 Filters per Feed. A word cannot be in both lists for the same place to look. Remove one with the menu. A Filter is only applied to Items the Feed finds from then on; an Item that a Filter held back is recorded as seen and is not posted later, even if you change the Filters.

## Role mentions

On the panel, press **Mentions** and choose up to 10 roles. Every message the Feed posts then pings them. Put `{{mentions}}` in the message text to choose where the pings go; without it they are put at the start of the message. The bot pings only the roles chosen there: never `@everyone`, `@here` or individual people, even if an Item's text contains them. To ping a role that is not set to "Allow anyone to @mention this role", the bot needs the Mention Everyone permission.

## Post as

Post as is the name and picture a Feed's messages appear under.

- **The bot** (default): the bot's own name and picture.
- **The site's name and icon**: the name and picture of the source. The name is the feed's title; the picture is the feed's own logo or, if it has none, the icon of the site's home page. The bot looks these up when you choose this option and again about once a week. If it finds no picture, only the name changes.
- **A custom name and picture**: a name you type (up to 80 characters) and an optional picture address. When you add a Feed, choose this afterwards with the **Post as** button, because the add form has no room for the name and picture.

For the last two the bot posts through a webhook called "RSS feeds" that it creates itself, one per channel and shared by the Feeds in that channel; it removes the webhook when no Feed in the channel needs it any more. This needs the **Manage Webhooks** permission. Without it, or when the channel is full of webhooks, the Feeds in that channel keep working but post as the bot, and the Logs channel is told once. Discord does not allow "discord" or "clyde" in these names, so the bot alters them slightly.

## Forum channels

A Feed can be bound to a forum channel. Each Item then becomes a new Forum post, with the Feed's message text, Embed and Buttons as the post's first message.

Press **Forum options** on the panel (it appears only for forum Feeds):

- **Post title.** A template for the Forum post's title; the default is `{{title||feed_title}}`. It is cut to Discord's limit of 100 characters. If it comes out empty, the Item's title or the Feed's name is used.
- **Tags.** Up to 5 of the forum's own tags, put on every Forum post. If the forum requires a tag on every post and the Feed has none, the Feed is paused and the Logs channel says so; choosing a tag resumes it.
- **Cover image.** When on, the Item's image is downloaded (PNG, JPEG, GIF or WebP, up to 8 MB) and attached to the Forum post so that it shows as the post's picture. If the image cannot be downloaded or attached, the Item is posted without it. This needs the Attach Files permission.

## OPML import and export

OPML is the file format most feed readers use to move a list of feeds.

**Import.** `/feed import` takes an OPML file (at most 1 MB) and an optional `channel` (the channel you are in if left out). It adds a Feed for each address in the file that the channel does not have yet, with the default settings: checked every 10 minutes, posted as the bot, default Template. Like `/feed add`, it does not post Items that are already in the sources. One import adds at most 100 Feeds, and a file can list at most 500; import the file again for the rest. The reply says how many were added, skipped (already in the channel), left out, and which failed and why. Files with a DOCTYPE or entity declaration are refused.

**Export.** `/feed export` sends `feeds.opml` with the name and address of every Feed in the Server. Channels, Templates and Filters are not included.

## How it behaves

- **Nothing is posted when a Feed is added.** Whatever the source lists at that moment is recorded as seen; only Items that appear afterwards are posted. The same goes for a Feed whose address you change.
- **At most 10 new Items per Check.** If more than ten new Items have appeared (after a long outage, say), the ten newest are posted, oldest first, and the rest are skipped for good. Items held back by Filters do not count towards the ten. A Check that runs short of time posts what it can and leaves the remaining Items for the next Check; that is not a failure.
- **An Item that cannot be posted is retried, then skipped.** If Discord or the network fails, the Item is tried again on the next Check, up to three attempts in all, and then it is skipped. If Discord refuses the customised message, the bot first tries once with the default message text, and skips the Item if that is refused too. Nothing else is held up. The Feed's status then reads "Working, 3 Items could not be posted since ..." on its panel and in `/feed list`, until the Feed next posts an Item without losing another; each skipped Item is also written to the bot's log.
- **An Item is never posted twice, at the price of an occasional lost one.** If the bot is cut off in the middle of sending an Item (it is stopped or crashes, or the Check runs out of time), it cannot know whether Discord took the message. It does not send that Item again: the Item is skipped and counted on the Feed's status like any other.
- **A Broken feed is retried with growing delays.** If a Check fails (the site cannot be reached, the answer is not a feed, or the Check takes over two minutes), the wait before the next one starts at the Feed's interval, doubles each time up to an hour, and stays at the Feed's interval if that is longer. If the site answers with error 429 or 503 and says how long to wait (Retry-After), the bot waits at least that long, up to 6 hours. Error 429 means the site wants fewer requests, not that the Feed is broken: it does not count as a failed Check, the Feed's status reads "Rate limited" until the site next gives any other answer, and the next Check comes when the site said (at least a minute later), or after the Feed's interval if it did not say. `/feed resume` and `/feed refresh` without a Feed keep to that wait; `/feed refresh` for the one Feed does not. A Broken feed is still checked, and `/feed list` shows it as failing, with the last error. If it keeps failing for a day, the bot posts one message about it in the Logs channel, and one more when it works again.
- **A lost channel pauses the Feed.** If the bot can no longer see or post in the channel (it was deleted, or the permission was taken away), the Feed becomes a Paused feed and the Logs channel is told. Restore access and press **Resume**, or move the Feed to another channel in its settings. Items that were not delivered are posted then if the source still lists them. An archived thread is opened again by the bot, unless it is locked.
- **Removing the bot from a Server stops its Checks at once, and its data is deleted 30 days later.** If the bot is invited back within the 30 days, the Feeds, Templates, Filters and access come back with it. A removal that happens while the bot is not running is noticed when it starts again.
- **Private network addresses are refused.** Feed addresses, redirects, site pages and images that lead to private, local or link-local addresses are not fetched, however they are written, unless you set `ALLOW_PRIVATE_URLS=true`. Addresses with a username or password in them are always refused. The bot reads at most 5 MB from a feed, follows at most 5 redirects, and waits at most 30 seconds for an answer.
- Several Feeds are checked at the same time (up to five), and each wait is stretched by up to a tenth so that Feeds drift apart instead of all being fetched together.

## Log entries and the Logs channel

Every change a member or the bot makes is saved as a **Log entry**: who did it, what, to which Feed, and when. Entries are kept for one year. Two kinds are kept for as long as they stay true: who added a Feed that still exists, and who paused a Feed that is still paused. When the bot is removed from a Server, its Log entries are deleted with the Server's other data 30 days later.

Where to see them:

- **On the Feed.** The Feed's panel shows `Added by @member` and, in its status, who paused a Paused feed. `/feed list` shows who paused a Paused feed.
- **`/feed history feed:<Feed>`** (Managers) lists one Feed's Log entries, newest first, under the header `History of <Feed>: <n> Log entries`. A Feed with none says "This Feed has no Log entries yet."
- **`/log [member] [kind]`** (Admins) lists the Log entries of the whole Server, including those of Feeds that were removed and of Grants. Give `member` to see what one member did, or `kind` (**Feeds added and removed**, **Pauses and resumes**, **Edits**, **Access** or **The bot's own reports**) to see one group of changes; both together show only entries that match both. The header reads `Log entries: <n>`, with the filters in brackets, e.g. `Log entries (by @alex · Edits): 4`. A Server with none says "This Server has no Log entries yet.", and filters that match nothing say "No Log entries match that." A member who has since left the Server is shown as `Name (left the Server)`, using the name saved with the entry.
- **The Logs channel**, if you set one with `/setup`. The bot posts a message there as an embed (a card) when:
  - a Feed is added, removed, paused, resumed or edited;
  - an OPML file is imported (one report that lists every Feed it added);
  - a Grant is given or taken away;
  - the Logs channel itself is changed;
  - the bot reports something of its own: it paused a Feed, a Feed became a Broken feed, or a Feed works again.

  Each card shows the member with their picture, what happened, the Feed's name and channel, `By @member` with their ID, and the time. Changes to a Template, a Filter, Post as, role mentions or forum tags are saved and shown in the history, but not posted in the Logs channel, because they are small and frequent. The bot needs the **Embed Links** permission in the Logs channel; without it, it posts the same report as plain text.

A Server with no Logs channel still has all its Log entries; only the posting is skipped.

## Reading the logs

The bot writes its own log to the container's output, one line each, and Docker keeps it. Read it with:

```
docker compose logs -f rssbot
```

`-f` follows new lines; leave it out to print what is there, add `--since 1h` or `--tail 200` to cut it down. Every line starts with the time in UTC, the level and the part of the bot that wrote it. After that comes a word saying what happened and `key=value` pairs. Names that a member or a feed supplied are in quotes, so a Feed called `x" by="someone` cannot fake a line.

`LOG_LEVEL` in `.env` chooses how much is written. Restart the bot after changing it (`docker compose up -d`).

| Level | What you get | Example |
| --- | --- | --- |
| `INFO` (default) | Start-up, connecting to Discord, joining and leaving Servers, every change a member makes, every command, button and form that is used, a Check that posted Items, and housekeeping that deleted something. Quiet: a Feed with nothing new writes nothing. | `2026-10-08T12:00:03Z INFO rssbot.scheduler: check feed=45 name="BBC News" server=123 posted=3 skipped=0 took_ms=420` |
| `WARNING` | Only trouble: a Check that failed, a Feed becoming Rate limited, a Skipped item and why, a delivery that failed, a Feed the bot paused or reported as a Broken feed (`feed.auto_pause`, `feed.broken`). | `2026-10-08T12:10:03Z WARNING rssbot.scheduler: check.failed feed=45 name="BBC News" server=123 failures=2 error="The site took too long to answer."` |
| `DEBUG` | Everything above, plus every Check (also "nothing new" and "not modified"), every fetch with its status, size and time, every delivery, and each pass of the Check loop with how many Feeds were due. Busy: with 40 Feeds it writes several lines a minute. | `2026-10-08T12:10:03Z DEBUG rssbot.fetch: fetch host="https://feeds.bbci.co.uk" status=304 bytes=0 took_ms=95 conditional=yes` |

A Feed the bot pauses or reports as a Broken feed is written at `WARNING`, so it stands out:

```
2026-10-08T12:10:03Z WARNING rssbot.journal: feed.auto_pause server=123 feed=45 name="BBC News" channel=678 by=bot detail="..."
```

The lines that record a change use the Log entry's kind as their first word, and a command or a button press starts with `command`:

```
2026-10-08T11:58:20Z INFO rssbot.journal: feed.pause server=123 feed=45 name="BBC News" channel=678 by="alex" by_id=987
2026-10-08T11:58:20Z INFO rssbot.commands: command name="/feed pause" server=123 channel=456 by="alex" by_id=987 outcome=ok
```

**Finding what a member did.** Every line about a member carries their Discord user ID as `by_id`. Copy the ID (Discord's Developer Mode adds **Copy User ID** to a member's menu), then:

```
docker compose logs rssbot | grep by_id=987
```

The same for a Feed: `grep feed=45`.

**No secrets in the log.** The bot token is never written. A Feed is logged by its number and name; where an address helps, only its site is shown (`https://example.com`), because a Feed's address can hold a private key. Web addresses in error messages are cut down to their site too.

**Rotation.** Docker's job, not the bot's. `compose.yaml` sets the `json-file` driver to keep at most 5 files of 10 MB for this container, so the log cannot fill the disk. Change `max-size` and `max-file` under `logging:` there if you want more history. The Log entries behind `/feed history` and `/log` are in the database, not in the container log, and are not lost when Docker rotates it.

## Data and backup

Everything the bot keeps is one SQLite database, `rssbot.db`, in the `/data` volume (the compose file calls it `rssbot-data`). It holds the Feeds, Templates, Filters, access Grants, Logs channels, the Log entries, the record of Items already seen and the webhook addresses the bot created. The bot token is not stored there, but the webhook addresses are secret, so keep backups private.

The database runs in WAL mode, so while the bot is running its data is spread over `rssbot.db` and two side files. To back up, stop the bot first, then copy the volume. For example, with `docker volume ls` showing the volume as `discord-rss-bot_rssbot-data`:

```
docker compose stop rssbot
docker run --rm -v discord-rss-bot_rssbot-data:/data:ro -v "$PWD":/backup alpine tar czf /backup/rssbot-data.tgz -C /data .
docker compose start rssbot
```

To restore, put the files back in the volume while the bot is stopped.

## Development

The tests run in Docker; nothing needs to be installed on the host.

```
docker build --target test .
```

That builds the test stage, which runs `ruff check` and then `pytest` and fails if either does; it is also what CI runs. To run the tests against your working copy without rebuilding the image each time:

```
docker build --target dev -t rssbot-dev .
docker run --rm -v "$PWD":/app:ro rssbot-dev pytest -q
docker run --rm -v "$PWD":/app:ro rssbot-dev ruff check src tests
```

The code is in `src/rssbot`; `CONTEXT.md` has the vocabulary and `docs/adr/` the decisions behind the design. A tag such as `v0.1.0` makes GitHub Actions build the image for `linux/amd64` and `linux/arm64` and publish it to `ghcr.io/mmcmd/discord-rss-bot` as `0.1.0` and `latest`.

## Licence

This project is under the MIT licence with the Commons Clause. You may use it, change it and host it yourself for free, including inside a business. You may not sell it, and that includes offering it to others as a paid hosted service or charging for support or consulting whose value comes mainly from the bot. See `LICENSE` for the full text.
