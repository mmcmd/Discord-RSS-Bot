# An Item with no image takes the og:image of its own page

Some feeds, TechCrunch among them, carry no image at all: no media tags, no enclosure, no picture in the text. The picture only exists on the article's page, in its `og:image` tag, which is what Discord itself uses for link previews. So when an Item that is about to be posted has no image, the bot fetches the Item's link and uses the page's `og:image`.

Only Items about to be posted are looked up, and only when the Feed gave none, so Feeds that carry images cost nothing extra. Lookups run up to ten at a time, each with a five second limit, through the same fetcher as everything else, so the guard against internal addresses applies.

## Consequences

A Check makes up to one extra request per new Item of an imageless Feed, to the site the Feed already comes from. A page that is slow, blocked or without an `og:image` does not hold anything up: the Item is posted without an image, in line with docs/adr/0002. A site that rate-limits bursts may answer some lookups with an error, and those Items post without a picture.
