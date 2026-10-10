# An Item whose delivery was cut off is skipped, not sent again

When the bot is stopped, crashes or runs out of time while an Item is being sent, it cannot know whether Discord took the message. We chose to make that Item a Skipped item instead of sending it again on the next Check: a duplicate in a channel is seen by everyone and looks like the bot is broken, while a lost Item is rare and is reported on the Feed's status.

## Consequences

An Item is marked as being sent before its delivery starts, and an Item found still marked that way is skipped. So an Item can be lost although Discord never received it, for example when the bot is restarted while a Cover image is still downloading. A delivery that Discord answered with an error is not affected: it is retried as before (see 0002).

The same holds when the connection to Discord fails or times out after the request went out: there is no answer, so the Item is skipped. Only an answer from Discord (an error status, including 5xx and 429) or a failure that provably happened before anything was sent, such as being unable to connect, is retried.

The bot's own sends give up on a rate-limit wait of more than 30 seconds, which is then retried on the next Check, so that no send sleeps past the time an Item is allowed. Webhook sends (Post as) are the exception: discord.py waits out a 429 on them itself, and cutting that wait short could post the Item twice, so a long wait on a webhook can use up the Item's time and the Item is skipped as cut off.
