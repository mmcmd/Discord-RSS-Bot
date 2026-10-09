# Managers act with the bot's reach, not their own

A Manager may point a Feed at any channel the bot can post in, and may use any role mention the bot is allowed to make, whatever the Manager's own Discord rights in that channel or over that role. We chose not to check the Manager's own permissions because access to the bot is already a deliberate decision by an Admin (a Grant), and a second set of rules from Discord's channel and role settings would make "who can manage Feeds" hard to predict and to explain.

## Consequences

Granting Manager is granting the bot's reach. A Manager who cannot post in a private channel can still make a Feed post there if the bot can, and can make a Template mention a role they could not mention themselves. Admins who want to limit this limit the bot's own permissions (channel overrides, "Mention all roles") or grant Manager only to people they trust. Every such change is recorded in the Feed's history (see `CONTEXT.md`, Log entries), with who made it.
