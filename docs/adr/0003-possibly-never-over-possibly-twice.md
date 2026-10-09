# An Item whose delivery was cut off is skipped, not sent again

When the bot is stopped, crashes or runs out of time while an Item is being sent, it cannot know whether Discord took the message. We chose to make that Item a Skipped item instead of sending it again on the next Check: a duplicate in a channel is seen by everyone and looks like the bot is broken, while a lost Item is rare and is reported on the Feed's status.

## Consequences

An Item is marked as being sent before its delivery starts, and an Item found still marked that way is skipped. So an Item can be lost although Discord never received it, for example when the bot is restarted while a Cover image is still downloading. A delivery that Discord answered with an error is not affected: it is retried as before (see 0002).
