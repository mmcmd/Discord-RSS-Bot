# Discord RSS Bot

A self-hosted Discord bot that posts new items from RSS and Atom feeds into channels, with a customisable message per feed.

## Language

### People and access

**Instance**:
One running copy of the bot, serving one or more Servers.

**Deployer**:
The person who runs an Instance. A Deployer has no standing inside Discord.
_Avoid_: Operator, host, bot owner

**Server**:
A Discord server the Instance has been invited to.
_Avoid_: Guild

**Admin**:
A member who may give and take away access in a Server and do everything a Manager can. The server owner, anyone with Discord's Administrator permission, and anyone holding an Admin Grant are Admins.

**Manager**:
A member who may manage Feeds in a Server.
_Avoid_: Moderator, editor

**Grant**:
Admin or Manager access given to one role or one member in one Server.
_Avoid_: Permission, assignment

### Feeds and items

**Feed**:
A source URL bound to one channel, with its own name, Template, Filters and Check interval. The same URL in two channels is two Feeds.
_Avoid_: Subscription, source, connection

**Item**:
One entry published by a Feed's source.
_Avoid_: Article, entry, post, story

**Check**:
One attempt to fetch a Feed and post its new Items. "Refresh" is only the name of the command and button with which a Manager asks for a Check now.
_Avoid_: Poll, scan, sync

**Filter**:
A word tied to a Feed that an Item must contain (must-have) or must not contain (block), optionally limited to one field of the Item.
_Avoid_: Rule, keyword

### Messages

**Template**:
A Feed's layout for what is posted: message text, an Embed, or both, and Buttons.
_Avoid_: Format, layout

**Placeholder**:
A `{{name}}` token in a Template that is replaced by a field of the Item.
_Avoid_: Variable, tag, token

**Fallback**:
A Placeholder that names several fields and uses the first one that is not empty.

**Embed**:
The optional boxed card posted under a Feed's message text, or alone when the message text is empty.
_Avoid_: Card

**Field**:
A labelled name and value box inside an Embed.

**Button**:
A link button posted under a Feed's message.

**Post as**:
The name and picture a Feed's messages appear under: the bot's own, the site's, or a custom one.
_Avoid_: Identity, persona, webhook profile

**Forum post**:
The thread created for one Item when its Feed is bound to a forum channel.
_Avoid_: Thread (for this meaning)

**Cover image**:
An Item's image attached to its Forum post so that it shows as the post's picture.

### Delivery states

**Seen item**:
An Item the bot has recorded, so that it is never posted again.

**Skipped item**:
An Item the bot gave up posting, because it fell outside Catch-up, its delivery kept failing, or its delivery was cut off before the bot knew whether Discord took it.
_Avoid_: Dropped item, failed item

**Catch-up**:
The limit on how many new Items a single Check may post.

**Broken feed**:
A Feed whose Checks keep failing. It is still checked.
_Avoid_: Dead feed, failed feed

**Rate-limited feed**:
A Feed whose source last answered by asking the bot to wait (error 429). It is checked again when the source said. It is not a Broken feed.
_Avoid_: Slowed feed, throttled feed

**Paused feed**:
A Feed that is not checked until it is resumed.
_Avoid_: Disabled feed, stopped feed

### Records

**Log entry**:
A record of one thing a member or the bot did: who, what, which Feed, and when.
_Avoid_: Action, event, audit record

**Logs channel**:
The optional channel in a Server where the bot reports what members did to Feeds and access, and Feed problems.
_Avoid_: Alerts channel, audit channel
