# An Item that cannot be posted is skipped, not waited for

The bot exists because its predecessor stopped posting everything when one Item failed. So an Item whose delivery keeps failing becomes a Skipped item after a few attempts, and a Check never holds later Items or other Feeds behind it. We chose losing an occasional Item over guaranteed delivery, because guaranteed delivery means one bad Item can stall a Feed indefinitely.

## Consequences

Items can be posted out of order when an earlier one is retried, and an Item can be lost for good. Catch-up follows the same rule: Items beyond the limit are skipped instead of queued. A Feed that gave up on Items shows as failing, with their number, until it next posts one.
