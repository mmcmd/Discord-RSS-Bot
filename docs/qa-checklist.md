# Manual QA checklist

Click through this in a real Server with the bot running. Each line is: what to do -> what you should see. Every reply from the bot is private (only you see it) unless the line says it is posted in a channel. About 30 to 45 minutes; sections 9 and 11 need a few waits, so start a Feed for it early.

Words with capital letters (Feed, Item, Manager, Grant and so on) are defined in `CONTEXT.md`.

## 1. Before you start

- [ ] A test Server where you are the owner, with the bot invited and running (`docker compose logs -f rssbot` open in a terminal).
- [ ] A second account (an alt) in the Server with no Grant, no Administrator permission and none of the roles below.
- [ ] A role, call it `feed-managers`, to give a Manager Grant to. A second role you can delete, call it `temp`.
- [ ] Channels: one text channel the bot can post in; one private channel the bot cannot see; an announcement channel; a thread; a forum channel without a required tag and, if you can, one that requires a tag.
- [ ] Addresses: two or three real feeds, one of them a Reddit feed (`https://www.reddit.com/r/<name>/.rss`) or a YouTube feed, and one that posts often; one normal web page (`https://example.com`); one that gives a 404 (`https://example.com/nothing.xml`).
- [ ] An OPML file listing a dozen or so feeds (export one from any feed reader), so that the Server ends up with more than 10 Feeds.

## 2. Access

- [ ] `/access list` on a fresh Server -> "No Grants yet." and the line about the server owner and Administrators.
- [ ] `/access grant target:@feed-managers level:Manager` -> "Granted Manager access to @feed-managers."
- [ ] The same command again -> the same sentence plus "It already had that Grant."
- [ ] `/access grant target:<the alt> level:Admin` -> "Granted Admin access to ..."; then again with `level:Manager` -> "... It was Admin before."
- [ ] `/access grant target:@everyone level:Manager` -> "The bot cannot grant access to @everyone: that would give it to every member. Choose a role or a member instead."
- [ ] `/access grant target:<the bot> level:Manager` -> "Bots cannot be given access. Pick a role or a person instead."
- [ ] `/access grant target:@temp level:Manager`, delete the role `temp` in Server settings, `/access list` -> under **Managers**, Roles shows the role's number followed by "(deleted role)"; Members shows the alt as a mention followed by its ID.
- [ ] `/access revoke target_id:<that role number>` -> "Took away the Grant of `<number>`."; the same again -> "`<number>` has no Grant to take away."
- [ ] `/access revoke target_id:12345678901234567890` (20 digits) -> "That is not a valid ID." and not "Something went wrong".
- [ ] `/access revoke` with no options -> "Pick a role or member, or give the ID of one that no longer exists."
- [ ] As the alt (a Manager now): `/access list` and `/setup` -> "Only Admins of this Server can do that."
- [ ] As the alt: `/feed list` -> works (it is a Manager). Run `/feed add` far enough to have one Feed, and leave its panel on screen.
- [ ] As owner: `/access revoke target:<the alt>` -> "Took away the Grant of ...". As the alt: press **Pause** on the panel that is still on screen -> "Only Managers and Admins of this Server can do that." and the Feed is not paused.
- [ ] As the alt (no Grant): `/feed list`, `/feed add`, `/feed edit`, `/feed remove`, `/feed pause`, `/feed resume`, `/feed test`, `/feed import`, `/feed export`, each `/template` subcommand and `/filter` -> the same refusal every time; `/feed add` opens no form.
- [ ] As the alt: start typing in the `feed` option of `/feed edit` -> the list stays empty.

## 3. Setup

- [ ] `/setup` -> "**Logs channel**: none set", a menu **Choose the Logs channel** and a greyed-out **Clear**.
- [ ] Choose the text channel -> the panel now names it; **Clear** is usable.
- [ ] Choose the private channel the bot cannot see -> "**Warning**: the bot cannot see or post in #... Nothing will be reported until the bot is given access to it."
- [ ] Press **Clear** -> back to "none set".
- [ ] Choose a channel, delete that channel, run `/setup` -> "**Warning**: that channel no longer exists. Choose another one or clear it."; choosing another channel from that same panel works. Leave a working Logs channel set.

## 4. Feeds

Adding and the problem cases:

- [ ] `/feed add` -> a form **Add a Feed** with Feed address, Channel (pre-set to where you are), Check every (10 minutes) and Post as (The bot). Submit a real feed -> "Added the Feed **...** in #... Its source lists N Items now. None of them will be posted: only Items published from now on are." above the Feed panel.
- [ ] `/feed add` with the web page address -> "That address returned a web page, not an RSS or Atom feed." followed by the address you typed in brackets.
- [ ] `/feed add` with the 404 address -> "The site has nothing at that address (error 404), so check the address." followed by the address.
- [ ] `/feed add` with the same address and channel as the first Feed -> "That channel already has a Feed for that address." followed by the address.
- [ ] `/feed add` with the same address in another channel -> accepted (a second Feed).
- [ ] `/feed add` with Channel set to the private channel -> accepted; the panel ends with "**Warning**: the bot cannot see or post in #... Nothing will be posted until the bot is given access to it."
- [ ] `/feed add` with Post as **A custom name and picture** -> the note adds "It posts as the bot for now. Press **Post as** below to enter the custom name and picture."
- [ ] Add one Feed each in the announcement channel, the thread and the forum channel -> all accepted; the forum Feed's panel says "(forum)" and has a **Forum options** button.

The Feed panel (open one with `/feed edit feed:<name>`):

- [ ] The panel shows Feed, Address, Channel, Check interval, Status ("Working"), Last checked ("... ago"), Next Check ("in ..."), Post as, Message text, Embed, and the counts of Filters, Fields, Buttons and Mention roles.
- [ ] **Settings** -> form **Feed settings** (Name, Feed address, Channel, Check every). Change the name and the interval, submit -> the same message shows the panel with the new values.
- [ ] **Settings**, then close the form without submitting -> nothing changes and the panel still works.
- [ ] **Settings**, set the address to the web page address -> the error sentence as a new message; the panel is unchanged.
- [ ] **Mentions** -> "Choose the roles **...** mentions with every Item it posts, up to 10." with a menu **Choose the roles to mention**, **Clear** (greyed out) and **Back to Feed**. Pick a role -> the panel returns with Mention roles: 1.
- [ ] **Mentions**, pick only `@everyone` -> the panel returns with "`@everyone` cannot be a mention role, so it was left out." on top and no role added.
- [ ] **Mentions** -> **Clear** -> the panel returns with Mention roles: 0. **Mentions** -> **Back to Feed** -> the panel, nothing changed. Then set one mention role again for the tests below.
- [ ] **Post as** -> menu **Choose who the Feed posts as** and **Back to Feed**. Pick **The site's name and icon** -> the panel shows "The site's name and icon (<site name>)".
- [ ] **Post as** -> **A custom name and picture** -> form **Post as a custom name** (Name, Picture address). Submit a name and a picture address -> the panel shows "A custom name and picture (<name>)".
- [ ] **Post as** -> **The bot** -> the panel shows "The bot". **Post as** -> **Back to Feed** -> the panel.
- [ ] On the forum Feed: **Forum options** -> Forum post title, Tags, Cover image, a menu **Choose the tags put on every Forum post**, **Post title**, **Cover image: Off** and **Back to Feed**. Pick a tag -> the panel shows Tags: 1.
- [ ] **Forum options** -> **Post title** -> form **Forum post title**; submit `{{title}} ({{feed_title}})` -> the panel shows it.
- [ ] **Forum options** -> **Cover image: Off** -> the panel shows Cover image: on; **Forum options** now has **Cover image: On**. **Back to Feed** -> the panel.
- [ ] **Test** -> two new messages: the newest Item as it would look, then "Above is the newest Item of **...** as it would be posted in #... Nothing has been posted.", a numbered list of the five newest Items with the first in bold, buttons **1** to **5** (**1** greyed out), **Post to channel** and **Back to Feed**. With a mention role set, the note ends "Posting it will mention @role."
- [ ] Press **3** -> two more messages: the third Item as it would look, then "Above is Item 3 of **...**" with the third line in bold and **3** greyed out. **Post to channel** there posts the third Item.
- [ ] **Post to channel** -> "Posted the Item in #..." with **Back to Feed**; the Item is in the channel and the role was pinged. **Back to Feed** -> the panel.
- [ ] **Test** on the Feed in the private channel, then **Post to channel** -> "The bot cannot post in #... Check that the channel still exists and that the bot may see it and post in it." with **Try again** and **Back to Feed**; both work.
- [ ] **Pause** -> Status "Paused: by a member" and the button is now **Resume**. **Resume** -> Status "Working" and the button is **Pause** again.
- [ ] **Remove** -> "Remove the Feed **...** from #...? Its Template and Filters are removed with it. This cannot be undone." with **Remove** and **Cancel**. **Cancel** -> the panel is back.
- [ ] **Remove** -> **Remove** -> "Removed the Feed **...** from #..." with no buttons.
- [ ] **Message text**, **Embed**, **Fields**, **Buttons** and **Filters** each open their screen (sections 6 and 7 go through them).

The other `/feed` commands:

- [ ] `/feed pause feed:<name>` -> "Paused the Feed **...**. It is not checked until it is resumed." `/feed resume feed:<name>` -> "Resumed the Feed **...**. It is checked again from now on."
- [ ] `/feed refresh feed:<name>` -> "Refreshed the Feed **...**. Any new Items are now in #..."; a new Item published since the last Check is posted at once. **Refresh** on the panel does the same and shows the panel again. For a Feed with a broken address the reply ends with "Failing: " and the error, and its panel also shows **Last worked**.
- [ ] `/feed refresh` with no Feed -> "N Feeds will be refreshed, usually within a minute. Paused feeds are left out." and new Items show up within about a minute. On a Paused feed, `/feed refresh feed:<name>` -> "That Feed is paused. Resume it with `/feed resume` to refresh it." and its panel has no **Refresh**.
- [ ] `/feed test feed:<name>` -> the same preview and note as the **Test** button.
- [ ] `/feed remove feed:<name>` -> the remove question with **Remove** and **Cancel**. **Cancel** -> "Cancelled." and the Feed is still there.
- [ ] `/feed import file:<the OPML file>` -> "Imported the file into #..." with **Added**: N and the names; importing the same file again -> **Added**: 0 and **Skipped**: N.
- [ ] `/feed list` (more than 25 Feeds now; `/feed import` gets there quickly) -> "**Feeds in this Server**: N", followed by a count of each status if they are not all working; one block per Feed with a blank line between: name, channel and status, and under it in small grey text "checked ... ago"; "Page 1 of 2", a menu **Open a Feed's panel**, **Previous** (greyed out) and **Next**. **Next** -> page 2; **Previous** -> page 1.
- [ ] Pick a Feed in **Open a Feed's panel** -> its panel appears as a new message; the list above it still works.
- [ ] `/feed export` -> "This Server's Feeds as an OPML file." with `feeds.opml` attached; it lists every Feed's name and address.
- [ ] Give two Feeds in the same channel the same name (**Settings**), then type that name in the `feed` option -> the two entries differ (the site's host is shown after the name).
- [ ] With those two Feeds, `/feed list` -> in the menu **Open a Feed's panel** the two entries differ: under the name each shows the channel and then the site's host ("#... · example.com").
- [ ] Type part of a Feed's address (for example `reddit`) in the `feed` option -> that Feed is offered.

## 5. Stale screens

- [ ] Run `/feed edit` twice for the same Feed (two panels). Remove the Feed from one. Press any button on the other -> that panel is replaced by "That Feed no longer exists." with no buttons.
- [ ] Open `/feed list`. Remove a listed Feed with `/feed remove`. Pick that Feed in the old list's menu -> the list is redrawn without it, with "That Feed no longer exists." on top.
- [ ] Run `/filter` twice for a Feed that has a Filter word. Remove the word in one. Pick it in **Remove a word** in the other -> the list is redrawn with "That word was already removed."
- [ ] Run `/template fields` twice for a Feed with two Fields. Remove Field 1 in one. Pick any Field in **Remove a Field** in the other -> "The list has changed. Choose again." and nothing more is removed.
- [ ] The same with `/template buttons` and **Remove a Button** -> "The list has changed. Choose again."

## 6. Templates

- [ ] `/template text feed:<name>` -> form **Message text: <name>**. Submit `**{{title}}** by {{author}}` and a second line `{{link}}` -> "Saved the message text of **...**.", a preview of the newest Item, **Edit again** and **Back to Feed**. **Edit again** reopens the form with your text; **Back to Feed** shows the panel.
- [ ] Submit the form empty on a Feed with no Embed -> "**...** has no message text now. Until it has an Embed, the default message text is posted." with **Edit again**, **Add Embed** and **Back to Feed**.
- [ ] Submit `Hello {{foo}}` -> a sentence naming `"foo" in {{foo}}` and the valid names, "Nothing was saved. This is what you sent, so that you can copy it:" with your text in a box, **Edit again** and **Back to Feed**.
- [ ] `/template embed feed:<name>` -> form **Embed: <name>** (Title, Description, Link, Image, Footer). Submit title `{{title}}`, description `{{description:300}}`, link `{{link}}`, image `{{image}}`, footer `{{feed_title}} · {{date}}` -> "Saved the Embed of **...**." with a preview, a menu **Choose a colour**, **Edit again**, **Fields**, **Remove Embed** and **Back to Feed**.
- [ ] In that preview the footer shows a readable date such as "8 Oct 2026 14:05 UTC", not `<t:...>`.
- [ ] Pick **Green** in **Choose a colour** -> "The colour of the Embed of **...** is now Green."; pick **No colour** -> "The Embed of **...** has no colour now."
- [ ] `/template embed feed:<name> colour:#ff8800` and submit -> the preview has an orange edge. `colour:orange` -> "The colour must be a hex code such as #ff8800, or none for no colour." and no form.
- [ ] Embed form with link `{{title}}` -> "The Embed link must start with http://, https:// or one of the Placeholders that hold a web address: {{link}}, {{image}} or {{feed_link}}." with what you typed, **Edit again** and **Back to Feed**.
- [ ] **Remove Embed** -> the same message becomes "Remove the Embed of **...**?" with **Remove Embed** and **Cancel** (no preview, no second message). **Cancel** -> "Kept the Embed of **...**." with the preview and the same controls as before. Again, **Remove Embed** twice -> "Removed the Embed of **...**." in the same message, with **Add Embed** and **Back to Feed**.
- [ ] `/template fields feed:<name>` -> "**Fields of the Embed of ...** (0 of 25)", "There are no Fields yet.", **Add Field**, **Edit Embed** and **Back to Feed**.
- [ ] **Add Field** -> form **Add a Field** (Name, Value, Side by side). Submit `Author` / `{{author||feed_title}}` -> "Added Field 1." and a menu **Remove a Field**. Add a second one with Side by side **Yes** -> its line ends "(side by side)".
- [ ] **Add Field** with value `{{foo}}` -> the error, what you typed, **Try again** and **Back to Fields**; **Back to Fields** shows the list.
- [ ] **Remove a Field**, pick one -> "Removed Field N." **Edit Embed** opens the Embed form; **Back to Feed** shows the panel.
- [ ] `/template buttons feed:<name>` -> "**Buttons of ...** (0 of 5)", **Add Button** and **Back to Feed**. **Add Button** -> form **Add a Button** (Label, Address already `{{link}}`). Submit label `Read` -> "Added Button 1." and a menu **Remove a Button**.
- [ ] **Add Button** with address `{{title}}` -> "The Button address must start with http://, https:// or one of the Placeholders that hold a web address: {{link}}, {{image}} or {{feed_link}}." with **Try again** and **Back to Buttons**.
- [ ] **Add Button** with label `Search` and address `https://www.google.com/search?q={{title}}` -> accepted. **Test** -> **Post to channel**; press **Search** on the posted message -> Google searches for the whole title, spaces included.
- [ ] **Remove a Button**, pick one -> "Removed Button N."
- [ ] Set the message text to `{{description}}`, `{{link}}`, `{{mentions}}` on three lines, on a Feed with a mention role whose newest Item has a very long description. **Test** -> **Post to channel** -> the posted message is shortened inside the description, still ends with the link, and pings the role.
- [ ] On the Reddit Feed, set the message text to `{{description}}` and **Test** -> "[link]" and "[comments]" show as working links, not as broken brackets.
- [ ] With a Feed whose newest Item's title contains `*`, `_` or starts with `#` (or any Feed, after checking the rule on a title you know): **Test** -> the title shows those characters as written, with no italics and no heading.
- [ ] `/template placeholders feed:<name>` -> every Placeholder from `{{title}}` to `{{mentions}}` with its value for the newest Item, then three lines about Fallback, length limit and `{{url}}`.
- [ ] `/template reset feed:<name>` -> "Reset the Template of **...**? Its message text goes back to the default, and its Embed, Fields and Buttons are removed." with **Reset** and **Cancel**. **Cancel** -> "Cancelled." and nothing changed.
- [ ] `/template reset` again, **Reset** -> "The Template of **...** was reset: the default message text, no Embed and no Buttons." with **Back to Feed**; the panel shows Embed: no, Fields: 0, Buttons: 0.

## 7. Filters

- [ ] `/filter feed:<name>` -> "**Filters for ...**", an explanation that mentions accents and endings ("“cafe” does not match “Café”, “bank” does not match “Banks”") and "Changes apply to new Items only", both lists "none", **Add must-have words**, **Add block words** and **Back to Feed**.
- [ ] On a Japanese or Chinese Feed, add a must-have word that appears inside a sentence without spaces around it (for example `東京`) -> an Item whose title contains it is posted; one without it is not.
- [ ] **Add must-have words** -> form **Add must-have words** (Words, Look in). Submit two words on two lines -> "Added 2 must-have words: “...”, “...”." and both in the list.
- [ ] **Add block words**, submit one line `red, blue` -> "Added 1 block word: “red, blue”." (one phrase, not two words).
- [ ] **Add block words** with Look in **Title** -> the word is listed with "(title)".
- [ ] **Add block words** with a word that is already a must-have word (same Look in) -> "“...” is already a must-have word for this Feed. Remove it there first." and nothing added.
- [ ] Submit a word that is already in the same list -> "Nothing was added: the words were blank or already in the list."
- [ ] Add a block word that the Feed's newest Item contains, then **Test** on the panel -> the preview is of the newest Item without the word ("Above is the newest Item your Filters let through of **...**"), no number stands for the blocked Item, and the note has "Held back by your Filters: *<its title>*." Add a must-have word that no Item contains -> **Test** shows no preview and no **Post to channel**: "Your Filters hold back all N Items that **...** lists right now, so there is nothing to test." Remove both words again.
- [ ] **Remove a word**, pick one -> "Removed “...” from the block words." **Back to Feed** -> the panel shows the new Filters count.
- [ ] On a Feed that posts often, add a block word that appears only inside link addresses of its Items (for example `www` or `reddit`) -> new Items are still posted.
- [ ] Add a block word that appears in the visible title of most Items -> those Items are not posted, and they do not appear later when you remove the word.

## 8. Help

- [ ] As owner: `/help` -> a card titled **Feeds** with six commands from `/feed add` to `/feed resume`, "Page 1 of 2" at the bottom, buttons **Feeds** (coloured, cannot be pressed), **Templates**, **Admin**, and under them **◀** (greyed out) and **▶**.
- [ ] Commands with options show them after the name (`/feed edit` then `<feed>`) and an "Example:" line; `/feed add` and `/feed list` have neither.
- [ ] **▶** -> "Page 2 of 2" with `/feed refresh` to `/filter`; **▶** is greyed out and **◀** goes back.
- [ ] **Templates** -> the six `/template` commands, no page number and no arrows. **Admin** -> `/setup` and the three `/access` commands, no arrows.
- [ ] From page 2 of Feeds, press **Templates** and then **Feeds** -> Feeds opens on page 1.
- [ ] As the alt with no Grant: `/help` -> "You need to be given access by an Admin of this Server to use this bot." and no buttons.
- [ ] `/access grant target:<the alt> level:Manager`. As the alt: `/help` -> only **Feeds** and **Templates**; no **Admin** button. Leave it on screen.
- [ ] As owner: `/access revoke target:<the alt>`. As the alt: press **Templates** on the help still on screen -> it is replaced by the "You need to be given access" sentence, with no buttons.
- [ ] Restart the bot (`docker compose restart rssbot`), then press a button on a `/help` opened before the restart -> it still works.
- [ ] In a direct message with the bot, `/help` is not offered.

## 9. Delivery (watch the channels)

- [ ] Right after `/feed add`, the channel stays empty: no old Items are posted.
- [ ] On a Feed that posts often with a 5 minute interval, a real new Item appears in the channel within about 5 minutes of being published, once.
- [ ] The forum Feed creates a new Forum post per Item, with the chosen tag, the Post title you set and, with Cover image on, the Item's picture as the post's picture.
- [ ] A Feed with Post as **The site's name and icon** posts under the site's name and icon; one with **A custom name and picture** posts under your name and picture.
- [ ] A Feed in the forum that requires a tag, with no tag chosen -> at its next Item the Feed is paused and the Logs channel says "The Feed **...** in #... was paused: that forum wants a tag on every Forum post and the Feed has none. Give the Feed a tag, then resume it." Choosing a tag under **Forum options** resumes it.
- [ ] Take away the bot's View Channel or Send Messages permission in a Feed's channel -> at its next new Item the Feed shows "Paused: the bot can no longer post in its channel" in `/feed list`, and the Logs channel gets ONE note: "The Feed **...** in #... was paused: the bot can no longer post in that channel. Check the channel and the bot's permissions there, then resume the Feed."
- [ ] Before giving the permission back: `/feed resume feed:<name>` -> "Resumed the Feed **...**. It is checked again from now on." followed by "**Warning**: the bot cannot see or post in #... Nothing will be posted until the bot is given access to it."; its panel shows the same warning, also after **Resume** there.
- [ ] Give the permission back and press **Resume** -> Status "Working", no warning on the panel, and the Item that was held back is posted.
- [ ] `docker compose restart rssbot` -> nothing that was already posted is posted again, in any channel.
- [ ] `docker compose stop rssbot`, kick the bot from a second test Server (or this one, last), `docker compose start rssbot` -> the log says `server.removed server=<number>` with `noticed=on_start` and no Feed of that Server is checked or posted.

## 10. Only provable in live Discord

- [ ] **Forum options**: deselect every tag in **Choose the tags put on every Forum post** -> Discord lets you send the empty choice and the panel shows Tags: 0.
- [ ] **Mentions**: deselect every role in the menu -> the panel shows Mention roles: 0.
- [ ] The menus inside pop-up forms work: Channel and Check every in **Add a Feed** and **Feed settings**, Post as in **Add a Feed**, Side by side in **Add a Field**, Look in in the Filter forms. Each keeps its pre-set value and what you pick is what is saved.
- [ ] A form opened from a panel button replaces that panel on submit; the same form opened from a slash command (`/template text`) answers with a new message.
- [ ] Buttons on a panel still work after `docker compose restart rssbot` (press **Pause** on a panel that was open before the restart).
- [ ] No click anywhere in this checklist showed "This interaction failed", including **Post as** -> **The site's name and icon**, **Test**, **Post to channel** and submitting `/feed add` for a slow site. If one does, note the control and the time, and look for "No answer to ..." in the log.
- [ ] No reply showed "Something went wrong. It has been logged." If one does, note what you did and copy the error from the log.

## 11. Log entries, the Logs channel and the container log

Needs a Logs channel set in section 3, the bot allowed to post and embed there, and `docker compose logs -f rssbot` open. For the last lines, set `LOG_LEVEL=DEBUG` in `.env` and run `docker compose up -d`.

- [ ] `/feed add` for a working address -> in the Logs channel an embed titled "Feed added", green, whose author is you (with your picture). Its text is the Feed's name in bold and its channel ("**Name** in #channel"), the Feed's address, and "By @you `your ID`"; the embed carries a time.
- [ ] Pause the Feed, resume it, change its name in **Settings**, then remove it -> one embed each, with you as author, the Feed's name and channel, "By @you `your ID`" and a time. Titles and colours: "Feed paused" (grey), "Feed resumed" (green), "Feed edited" (blue, with a line such as "**Name**: Old -> New"), "Feed removed" (red).
- [ ] `/access grant` then `/access revoke` -> an embed for each, you as author: "Access given" with the role or member as a mention and a "**Level**: ... -> ..." line, and "Access taken away" naming the same target (both blue).
- [ ] Choose another Logs channel in `/setup` -> an embed titled "Logs channel changed" (blue) with a "**Logs channel**: #old -> #new" line, posted in the Server's Logs channel as it is after the change (the Log entry is saved either way). Press **Clear** -> the same title, with no new channel.
- [ ] Change a Feed's message text with `/template text`, add a Filter, change Post as, Mentions and forum tags -> nothing new appears in the Logs channel for any of them, yet each shows in `/feed history` for that Feed (as "changed the Template of this Feed", "changed the Filters of this Feed", "changed Post as of this Feed", "changed the mentions of this Feed", "changed the forum tags of this Feed").
- [ ] `/feed import` with the OPML file -> ONE report in the Logs channel titled "<n> Feeds imported" (green) that lists every Feed the import added as a "- **Name** in #channel" line (not one message per Feed), with you as author and one "By @you `your ID`".
- [ ] Take away the bot's Embed Links permission in the Logs channel, then pause a Feed -> the same report arrives as plain text, not an embed: a bold title, "by" you and the time on the first line, then the same lines. Give the permission back -> embeds again.
- [ ] Make the bot pause a Feed itself (take away its access to the channel as in section 9, wait for the next new Item) -> an amber embed titled "Feed paused by the bot" in the Logs channel, with the reason as its text, no member as author and no "By @member" line; the report is one message, not repeated on the following Checks. Restore access and resume -> the Feed works again; for a Broken feed (a bad address that failed for over a day) a red "Broken feed" embed was posted when it became one, and a green "Feed working again" embed comes when it works again.
- [ ] `/feed list` -> a Paused feed's status reads "Paused by @you · <time ago>" (or "Paused by the bot: <reason> · <time ago>" for the automatic pause). The list does not show who added a Feed. Resume the Feed -> "Paused by" is gone.
- [ ] A Feed's panel -> shows "**Added by**: @member · <date>"; paused, its **Status** line reads "Paused by @you · <time ago>".
- [ ] `/feed history feed:<a Feed you changed several times>` -> the header "**History of <Feed>**: <n> Log entries", then one line per Log entry, newest first, 10 to a page, each "<date> @member <what> · <what changed>" (for example "paused this Feed"), including the Template, Filter, Post as, mentions and tag changes that the Logs channel did not show. With more than 10, "Page 1 of 2" at the bottom and page buttons that turn the page. A member who has left the Server shows as "Name (left the Server)".
- [ ] `/feed history` for a Feed with no changes yet -> "This Feed has no Log entries yet.", not an error. As the alt with no Grant -> "Only Managers and Admins of this Server can do that."
- [ ] `/log` -> the header "**Log entries**: <n>", then the Server's Log entries, newest first, 10 to a page, each naming its Feed ("**Name** in #channel"), including those of the Feed you removed and of the Grants ("gave access", "took away access").
- [ ] `/log member:<the alt>` -> only entries made by the alt, header "**Log entries (by @alt)**: <n>". `/log kind:` offers five choices: Feeds added and removed, Pauses and resumes, Edits, Access, The bot's own reports; pick "Edits" -> only Feed, Template, Filter, Post as, mentions and forum tag changes, header "**Log entries (Edits)**: <n>". Both together -> only entries that match both, header "**Log entries (by @alt · Edits)**: <n>". A combination that matches nothing -> "No Log entries match that.", not an error. (On a Server with no Log entries at all, `/log` says "This Server has no Log entries yet.")
- [ ] As a Manager who is not an Admin: `/log` -> "Only Admins of this Server can do that."; `/feed history` still works.
- [ ] Container log, a command: run `/feed pause feed:<name>`, then `docker compose logs rssbot | grep by_id=<your user ID>` -> a `feed.pause` line (server, feed, name, channel, by, by_id) and a `command name="/feed pause" ... outcome=ok` line, both with your ID, each one line and timestamped in UTC.
- [ ] Container log, a refused command: as the alt with no Grant, run `/feed list` -> a `command name="/feed list" ... by_id=<the alt's ID> outcome=` line whose outcome is not `ok`, and no `feed.` line.
- [ ] Container log, a Check: with `LOG_LEVEL=INFO`, wait for a Feed to post a new Item -> one `check feed=<n> name="..." server=<n> posted=1 skipped=0 took_ms=...` line, and no lines at all for Feeds with nothing new. With `LOG_LEVEL=DEBUG` -> also a `tick due=` line on every pass, a `check` line for every Feed, and a `fetch host="https://..."` line with status, bytes, time and `conditional=` for every Check.
- [ ] Container log, trouble: add a Feed at `https://example.com/nothing.xml` by editing an existing Feed's address to it -> `check.failed feed=<n> ... failures=1 error="..."` at WARNING, then `failures=2` on the next Check.
- [ ] Container log, the bot's own reports: after the automatic pause above -> a `WARNING rssbot.journal: feed.auto_pause ... by=bot` line, while the `check feed=<n> ... paused=<reason>` line for that Check is not a WARNING (INFO if it posted Items, otherwise only with `LOG_LEVEL=DEBUG`). A member's `feed.pause` line stays at INFO.
- [ ] `docker compose logs rssbot | grep -i "key=\|token"` -> no Feed address with a key in it, no webhook address and no bot token anywhere in the output.
- [ ] Give a Feed the name `x" by="evil` -> in the container log its name is quoted with the quote escaped (`name="x\" by=\"evil"`); no extra `by=` pair or extra line appears.
- [ ] `docker compose restart rssbot` -> a `shutdown` line, then a `start version=... log_level=... data_dir=... allow_private_urls=...` line (no token), then a `ready bot="..." servers=<n> feeds=<n>` line.
