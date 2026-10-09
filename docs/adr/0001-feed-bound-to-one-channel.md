# A Feed is bound to one channel

A Feed is a source URL plus exactly one channel, and it owns its Template, Filters and Check interval. We chose this over a shared source with per-channel subscriptions because it matches how people think about the bot ("this feed posts here, looking like this") and keeps every setting in one place.

## Consequences

The same URL added to two channels is fetched twice and tracks its Seen items twice. That cost is accepted. Splitting Feed into a shared source and per-channel subscriptions later would mean migrating stored Feeds and Seen items.
