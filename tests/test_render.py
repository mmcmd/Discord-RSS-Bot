from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import pytest

from rssbot.models import (
    DEFAULT_FORUM_TITLE_TEMPLATE,
    DEFAULT_TEXT_TEMPLATE,
    ButtonSpec,
    ChannelKind,
    EmbedSpec,
    Feed,
    FieldSpec,
    Item,
    OutgoingMessage,
    PostAs,
)
from rssbot.render import hidden_buttons, render_default, render_item

ZWSP = "​"
IMG = "https://example.com/pic.png"


def make_feed(**changes: Any) -> Feed:
    base: dict[str, Any] = {
        "id": 1,
        "server_id": 10,
        "channel_id": 100,
        "channel_kind": ChannelKind.MESSAGES,
        "name": "My Feed",
        "url": "https://example.com/feed.xml",
        "interval_s": 600,
        "source_title": "Example Blog",
        "source_link": "https://example.com",
        "text_template": DEFAULT_TEXT_TEMPLATE,
        "embed": None,
        "buttons": (),
        "mention_role_ids": (),
        "post_as": PostAs.BOT,
        "custom_name": "",
        "custom_avatar": "",
        "site_name": "",
        "site_icon": "",
        "site_checked_at": None,
        "forum_title_template": DEFAULT_FORUM_TITLE_TEMPLATE,
        "forum_tag_ids": (),
        "forum_cover": False,
        "paused": None,
        "etag": None,
        "last_modified": None,
        "next_check_at": 0,
        "fail_count": 0,
        "skipped_count": 0,
        "skipped_since": None,
        "failing_since": None,
        "warned": False,
        "last_error": "",
        "last_success_at": None,
        "last_checked_at": None,
        "rate_limited_since": None,
        "created_at": 0,
    }
    base.update(changes)
    return Feed(**base)


def make_item(**changes: Any) -> Item:
    base: dict[str, Any] = {
        "key": "k1",
        "title": "Hello world",
        "link": "https://example.com/hello",
        "summary": "A short summary.",
        "content": "The full content.",
        "author": "Ada",
        "published": 1_700_000_000,
        "categories": ("news", "tech"),
        "image": IMG,
    }
    base.update(changes)
    return Item(**base)


EMPTY_ITEM = Item(
    key="", title="", link="", summary="", content="", author="", published=None,
    categories=(), image="",
)  # fmt: skip

BOTH = [render_item, render_default]


def embed_total(embed: EmbedSpec) -> int:
    return (
        len(embed.title)
        + len(embed.description)
        + len(embed.footer)
        + sum(len(f.name) + len(f.value) for f in embed.fields)
    )


def assert_acceptable(message: OutgoingMessage) -> None:
    """Discord's structural limits."""
    assert len(message.content) <= 2000
    assert message.content.strip() or message.embed is not None
    for text in (message.content, message.thread_title or ""):
        assert "@everyone" not in text
        assert "@here" not in text
    if message.embed is not None:
        e = message.embed
        assert e.title or e.description or e.image or e.footer or e.fields
        assert len(e.title) <= 256
        assert len(e.description) <= 4096
        assert len(e.footer) <= 2048
        assert len(e.fields) <= 25
        for f in e.fields:
            assert 1 <= len(f.name) <= 256
            assert 1 <= len(f.value) <= 1024
        assert embed_total(e) <= 6000
        for url in (e.url, e.image):
            assert url == "" or (url.startswith(("http://", "https://")) and len(url) <= 2048)
        assert e.colour is None or 0 <= e.colour <= 0xFFFFFF
    assert len(message.buttons) <= 5
    for b in message.buttons:
        assert 1 <= len(b.label) <= 80
        assert b.url.startswith(("http://", "https://"))
        assert len(b.url) <= 512
    if message.username is not None:
        assert 1 <= len(message.username) <= 80
        assert "discord" not in message.username.lower()
        assert "clyde" not in message.username.lower()
    if message.thread_title is not None:
        assert 1 <= len(message.thread_title) <= 100
        assert "\n" not in message.thread_title
    assert len(message.tag_ids) <= 5


# --- content ---


def test_default_template_content() -> None:
    message = render_item(make_feed(), make_item())
    assert message == OutgoingMessage(
        content="📰 | **Hello world**\nhttps://example.com/hello"
    )


def test_custom_text_template() -> None:
    feed = make_feed(text_template="{{author}}: {{title}} ({{categories}})")
    assert render_item(feed, make_item()).content == "Ada: Hello world (news, tech)"


def test_mentions_are_prepended_when_the_template_does_not_place_them() -> None:
    feed = make_feed(text_template="{{title}}", mention_role_ids=(5, 6))
    message = render_item(feed, make_item())
    assert message.content == "<@&5> <@&6> Hello world"
    assert message.mention_role_ids == (5, 6)


def test_mentions_stay_where_the_template_places_them() -> None:
    feed = make_feed(text_template="{{title}} cc {{mentions}}", mention_role_ids=(5,))
    assert render_item(feed, make_item()).content == "Hello world cc <@&5>"


def test_no_mentions_means_no_leading_space() -> None:
    feed = make_feed(text_template="{{title}}")
    message = render_item(feed, make_item())
    assert message.content == "Hello world"
    assert message.mention_role_ids == ()


def test_content_is_cut_to_2000_with_ellipsis() -> None:
    feed = make_feed(text_template="{{content}}")
    message = render_item(feed, make_item(content="x" * 5000, summary=""))
    assert len(message.content) == 2000
    assert message.content == "x" * 1999 + "…"


def test_content_of_exactly_2000_is_not_cut() -> None:
    feed = make_feed(text_template="{{content}}")
    message = render_item(feed, make_item(content="x" * 2000))
    assert message.content == "x" * 2000


def test_cut_strips_trailing_whitespace_before_the_ellipsis() -> None:
    feed = make_feed(text_template="{{content}}")
    text = "a" * 1990 + " " * 9 + "b" * 100
    message = render_item(feed, make_item(content=text))
    assert message.content == "a" * 1990 + "…"


def test_mentions_count_towards_the_2000() -> None:
    feed = make_feed(text_template="{{content}}", mention_role_ids=(5,))
    message = render_item(feed, make_item(content="x" * 5000))
    assert len(message.content) == 2000
    assert message.content.startswith("<@&5> xxx")
    assert message.content.endswith("x…")


def test_default_text_keeps_the_link_when_the_title_is_enormous() -> None:
    item = make_item(title="t" * 5000)
    for fn in BOTH:
        message = fn(make_feed(mention_role_ids=(5,)), item)
        assert len(message.content) <= 2000
        assert message.content.startswith("<@&5> 📰 | **ttt")
        assert message.content.endswith("…**\nhttps://example.com/hello")


def test_default_text_without_a_title_is_just_the_link() -> None:
    for fn in BOTH:
        assert fn(make_feed(), make_item(title="")).content == "https://example.com/hello"


def test_default_text_without_a_link() -> None:
    for fn in BOTH:
        assert fn(make_feed(), make_item(link="")).content == "📰 | **Hello world**"


# --- @everyone and @here ---


def test_everyone_and_here_are_broken_in_content() -> None:
    feed = make_feed(text_template="{{title}} @here")
    message = render_item(feed, make_item(title="hi @everyone and @here"))
    assert message.content == f"hi @{ZWSP}everyone and @{ZWSP}here @{ZWSP}here"


def test_everyone_is_broken_in_default_render() -> None:
    message = render_default(make_feed(), make_item(title="@everyone free nitro"))
    assert "@everyone" not in message.content
    assert f"@{ZWSP}everyone free nitro" in message.content


def test_everyone_is_broken_everywhere_else() -> None:
    feed = make_feed(
        channel_kind=ChannelKind.FORUM,
        forum_title_template="{{title}}",
        text_template="{{title}}",
        embed=EmbedSpec(
            title="{{title}}",
            description="{{title}}",
            footer="{{title}}",
            fields=(FieldSpec("{{title}}", "{{title}}"),),
        ),
        buttons=(ButtonSpec("{{title}}", "{{link}}"),),
    )
    message = render_item(feed, make_item(title="@everyone @here"))
    safe = f"@{ZWSP}everyone @{ZWSP}here"
    assert message.content == safe
    assert message.thread_title == safe
    assert message.embed is not None
    assert message.embed.title == safe
    assert message.embed.description == safe
    assert message.embed.footer == safe
    assert message.embed.fields == (FieldSpec(safe, safe),)
    assert message.buttons[0].label == safe


def test_other_at_words_and_role_mentions_are_untouched() -> None:
    feed = make_feed(text_template="{{title}}", mention_role_ids=(5,))
    message = render_item(feed, make_item(title="mail me@example.com @everybody"))
    assert message.content == "<@&5> mail me@example.com @everybody"


def test_source_text_cannot_add_mention_roles() -> None:
    feed = make_feed(text_template="{{title}}", mention_role_ids=(5,))
    message = render_item(feed, make_item(title="<@&999> {{mentions}}"))
    assert message.mention_role_ids == (5,)
    assert message.content == "<@&5> \\<@&999\\> {{mentions}}"


@pytest.mark.parametrize(
    ("title", "shown"),
    [
        ("Why 2 * 3 * 4", "Why 2 \\* 3 \\* 4"),
        ("# Big news", "\\# Big news"),
        ("- a list", "\\- a list"),
        ("snake_case and `code` and ~~gone~~", "snake\\_case and \\`code\\` and \\~\\~gone\\~\\~"),
        ("[a](b) > c || d \\ e", "\\[a\\](b) \\> c \\|\\| d \\\\ e"),
        ("see https://example.com/a_b_c now", "see https://example.com/a_b_c now"),
        ("Plain title, 100% (really)!", "Plain title, 100% (really)!"),
    ],
)
def test_formatting_characters_in_a_title_show_as_written(title: str, shown: str) -> None:
    feed = make_feed(
        channel_kind=ChannelKind.FORUM,
        forum_title_template="{{title}}",
        text_template="{{title}}",
        embed=EmbedSpec(
            title="{{title}}",
            description="{{title}}",
            footer="{{title}}",
            fields=(FieldSpec("{{title}}", "{{title}}"),),
        ),
        buttons=(ButtonSpec("{{title}}", "https://example.com/?q={{title}}"),),
    )
    message = render_item(feed, make_item(title=title))
    assert message.content == shown
    assert message.embed is not None
    assert message.embed.title == shown
    assert message.embed.description == shown
    assert message.embed.fields == (FieldSpec(shown, shown),)
    # Discord does not format these, so a backslash would show.
    plain = " ".join(title.split())
    assert message.thread_title == plain
    assert message.embed.footer == plain
    assert message.buttons[0].label == plain
    assert message.buttons[0].url == "https://example.com/?q=" + quote(plain, safe="")
    assert render_default(feed, make_item(title=title)).content.startswith(f"📰 | **{shown}**")


def test_a_cut_never_leaves_half_an_escaped_character() -> None:
    feed = make_feed(text_template="**{{title:4}}** / {{title}}")
    message = render_item(feed, make_item(title="ab*" * 1000))
    assert message.content.startswith("**ab…** / ab\\*ab")
    assert len(message.content) <= 2000
    assert not re.search(r"(?<!\\)(\\\\)*\\…", message.content)


# --- something to show ---


@pytest.mark.parametrize("fn", BOTH)
def test_item_with_every_field_empty_uses_the_feed_name(fn: Any) -> None:
    message = fn(make_feed(), EMPTY_ITEM)
    assert message.content == "My Feed"
    assert message.embed is None
    assert_acceptable(message)


@pytest.mark.parametrize("fn", BOTH)
def test_empty_item_and_empty_feed_name_still_shows_something(fn: Any) -> None:
    message = fn(make_feed(name="", source_title=""), EMPTY_ITEM)
    assert message.content == "New item"
    assert_acceptable(message)


def test_empty_item_with_a_full_custom_template() -> None:
    feed = make_feed(
        channel_kind=ChannelKind.FORUM,
        forum_cover=True,
        forum_title_template="{{title}}",
        text_template="{{title}} {{description}}",
        embed=EmbedSpec(
            title="{{title}}",
            description="{{description}}",
            url="{{link}}",
            image="{{image}}",
            footer="{{author}}",
            fields=(FieldSpec("Author", "{{author}}"), FieldSpec("{{date}}", "x")),
        ),
        buttons=(ButtonSpec("Read", "{{link}}"), ButtonSpec("{{title}}", "https://a.example")),
        post_as=PostAs.SITE,
        source_title="",
        mention_role_ids=(5,),
    )
    message = render_item(feed, EMPTY_ITEM)
    assert message.embed is None
    assert message.buttons == ()
    assert message.content == "<@&5> My Feed"
    assert message.thread_title == "My Feed"
    assert message.cover_image_url is None
    assert message.username == "My Feed"
    assert_acceptable(message)


def test_empty_content_falls_back_to_default_text() -> None:
    feed = make_feed(text_template="{{author}}")
    message = render_item(feed, make_item(author=""))
    assert message.content == "📰 | **Hello world**\nhttps://example.com/hello"


def test_whitespace_only_content_counts_as_empty() -> None:
    feed = make_feed(text_template="  \n {{author}} ")
    assert render_item(feed, make_item(author="")).content.startswith("📰 | **Hello world**")


def test_mentions_alone_are_not_something_to_show() -> None:
    feed = make_feed(text_template="{{mentions}} {{author}}", mention_role_ids=(5,))
    message = render_item(feed, make_item(author=""))
    assert message.content == "<@&5> 📰 | **Hello world**\nhttps://example.com/hello"


def test_empty_content_is_fine_when_there_is_an_embed() -> None:
    feed = make_feed(text_template="", embed=EmbedSpec(title="{{title}}"))
    message = render_item(feed, make_item())
    assert message.content == ""
    assert message.embed == EmbedSpec(title="Hello world")


def test_mentions_only_content_with_an_embed() -> None:
    feed = make_feed(
        text_template="", embed=EmbedSpec(title="{{title}}"), mention_role_ids=(5,)
    )
    assert render_item(feed, make_item()).content == "<@&5>"


def test_embed_that_renders_empty_triggers_the_content_fallback() -> None:
    feed = make_feed(text_template="", embed=EmbedSpec(title="{{author}}", url="{{link}}"))
    message = render_item(feed, make_item(author=""))
    assert message.embed is None
    assert message.content == "📰 | **Hello world**\nhttps://example.com/hello"


# --- Embed ---


def test_embed_renders_every_part() -> None:
    feed = make_feed(
        embed=EmbedSpec(
            title="{{title}}",
            description="{{description}}",
            url="{{link}}",
            image="{{image}}",
            footer="{{feed_title}}",
            colour=0x123456,
            fields=(
                FieldSpec("Author", "{{author}}", inline=True),
                FieldSpec("{{categories}}", "{{date}}"),
            ),
        )
    )
    assert render_item(feed, make_item()).embed == EmbedSpec(
        title="Hello world",
        description="A short summary.",
        url="https://example.com/hello",
        image=IMG,
        footer="Example Blog",
        colour=0x123456,
        fields=(
            FieldSpec("Author", "Ada", inline=True),
            FieldSpec("news, tech", "<t:1700000000:f>"),
        ),
    )


def test_embed_text_limits() -> None:
    feed = make_feed(
        embed=EmbedSpec(
            title="{{title}}",
            description="{{content}}",
            footer="{{author}}",
            fields=(FieldSpec("{{title}}", "{{author}}"),),
        )
    )
    # Kept under the 6000 total so only the per-part limits apply.
    item = make_item(title="t" * 300, content="c" * 3000, author="a" * 1100, summary="")
    embed = render_item(feed, item).embed
    assert embed is not None
    assert embed.title == "t" * 255 + "…"
    assert embed.description == "c" * 3000
    assert embed.footer == "a" * 1100
    assert embed.fields == (FieldSpec("t" * 255 + "…", "a" * 1023 + "…"),)


def test_embed_description_and_footer_limits() -> None:
    feed = make_feed(embed=EmbedSpec(description="{{content}}"))
    embed = render_item(feed, make_item(content="c" * 9000, summary="")).embed
    assert embed is not None
    assert embed.description == "c" * 4095 + "…"

    feed = make_feed(embed=EmbedSpec(footer="{{content}}"))
    embed = render_item(feed, make_item(content="c" * 9000, summary="")).embed
    assert embed is not None
    assert embed.footer == "c" * 2047 + "…"


def test_embed_total_shortens_the_description_first() -> None:
    feed = make_feed(
        embed=EmbedSpec(
            title="{{title}}",
            description="{{content}}",
            footer="{{author}}",
            fields=(FieldSpec("n", "v"),),
        )
    )
    item = make_item(title="t" * 256, content="c" * 4096, author="a" * 2048, summary="")
    embed = render_item(feed, item).embed
    assert embed is not None
    # 256 + 2048 + 2 leaves 3694 for the description.
    assert embed.description == "c" * 3693 + "…"
    assert embed.fields == (FieldSpec("n", "v"),)
    assert embed_total(embed) == 6000


def test_embed_total_then_drops_fields_from_the_end() -> None:
    fields = tuple(FieldSpec(f"name{i:02}", "{{content}}") for i in range(10))
    feed = make_feed(embed=EmbedSpec(description="{{summary}}", fields=fields))
    item = make_item(content="v" * 1024, summary="d" * 500)
    embed = render_item(feed, item).embed
    assert embed is not None
    # Each Field is 6 + 1024 = 1030; five fit in 6000 once the description is gone.
    assert embed.description == ""
    assert [f.name for f in embed.fields] == [f"name{i:02}" for i in range(5)]
    assert embed_total(embed) <= 6000


def test_embed_exactly_6000_is_untouched() -> None:
    feed = make_feed(embed=EmbedSpec(description="{{content}}", footer="{{author}}"))
    item = make_item(content="c" * 4096, author="a" * 1904, summary="")
    embed = render_item(feed, item).embed
    assert embed is not None
    assert embed_total(embed) == 6000
    assert embed.description == "c" * 4096


def test_at_most_25_fields() -> None:
    fields = tuple(FieldSpec(f"n{i}", "v") for i in range(40))
    embed = render_item(make_feed(embed=EmbedSpec(fields=fields)), make_item()).embed
    assert embed is not None
    assert [f.name for f in embed.fields] == [f"n{i}" for i in range(25)]


def test_fields_with_an_empty_name_or_value_are_dropped() -> None:
    fields = (
        FieldSpec("Author", "{{author}}"),
        FieldSpec("{{author}}", "value"),
        FieldSpec("  ", "value"),
        FieldSpec("Kept", "{{title}}", inline=True),
    )
    feed = make_feed(embed=EmbedSpec(fields=fields))
    embed = render_item(feed, make_item(author="")).embed
    assert embed is not None
    assert embed.fields == (FieldSpec("Kept", "Hello world", inline=True),)


def test_empty_fields_do_not_use_up_the_25() -> None:
    fields = tuple(FieldSpec("n", "{{author}}") for _ in range(30)) + (FieldSpec("last", "v"),)
    embed = render_item(make_feed(embed=EmbedSpec(fields=fields)), make_item(author="")).embed
    assert embed is not None
    assert embed.fields == (FieldSpec("last", "v"),)


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://example.com/a.png",
        "javascript:alert(1)",
        "example.com/a.png",
        "https://",
        "https://example.com/a b.png",
        "https://example.com/" + "a" * 2048,
        "not a url",
        "",
    ],
)
def test_embed_url_and_image_must_be_http_urls(bad: str) -> None:
    feed = make_feed(embed=EmbedSpec(title="T", url="{{link}}", image="{{image}}"))
    embed = render_item(feed, make_item(link=bad, image=bad)).embed
    assert embed == EmbedSpec(title="T")


def test_embed_url_of_2048_is_kept_and_http_is_allowed() -> None:
    url = "http://example.com/" + "a" * (2048 - len("http://example.com/"))
    feed = make_feed(embed=EmbedSpec(title="T", url="{{link}}", image="{{image}}"))
    embed = render_item(feed, make_item(link=url, image=url)).embed
    assert embed == EmbedSpec(title="T", url=url, image=url)


def test_embed_with_only_an_image_is_kept() -> None:
    feed = make_feed(embed=EmbedSpec(image="{{image}}"))
    assert render_item(feed, make_item()).embed == EmbedSpec(image=IMG)


def test_embed_that_renders_empty_is_left_out() -> None:
    feed = make_feed(
        embed=EmbedSpec(
            title="{{author}}",
            description=" {{author}} ",
            url="{{link}}",
            image="{{image}}",
            colour=5,
            fields=(FieldSpec("Author", "{{author}}"),),
        )
    )
    message = render_item(feed, make_item(author="", image=""))
    assert message.embed is None


def test_embed_colour_out_of_range_is_cleared() -> None:
    feed = make_feed(embed=EmbedSpec(title="T", colour=0x1000000))
    assert render_item(feed, make_item()).embed == EmbedSpec(title="T")
    feed = make_feed(embed=EmbedSpec(title="T", colour=-1))
    assert render_item(feed, make_item()).embed == EmbedSpec(title="T")


# --- Buttons ---


def test_buttons_render_label_and_url() -> None:
    feed = make_feed(buttons=(ButtonSpec("Read {{title}}", "{{link}}"),))
    assert render_item(feed, make_item()).buttons == (
        ButtonSpec("Read Hello world", "https://example.com/hello"),
    )


def test_button_label_is_cut_to_80() -> None:
    feed = make_feed(buttons=(ButtonSpec("{{title}}", "{{link}}"),))
    buttons = render_item(feed, make_item(title="t" * 200)).buttons
    assert buttons[0].label == "t" * 79 + "…"


def test_bad_buttons_are_dropped() -> None:
    feed = make_feed(
        buttons=(
            ButtonSpec("No label url", "{{feed_link}}"),
            ButtonSpec("{{author}}", "https://example.com"),
            ButtonSpec("Mail", "mailto:a@example.com"),
            ButtonSpec("Long", "https://example.com/" + "a" * 512),
            ButtonSpec("Good", "https://example.com/" + "a" * (512 - 20)),
        )
    )
    buttons = render_item(feed, make_item(author="")).buttons
    assert [b.label for b in buttons] == ["No label url", "Good"]
    assert len(buttons[1].url) == 512

    feed = make_feed(source_link="", buttons=(ButtonSpec("Site", "{{feed_link}}"),))
    assert render_item(feed, make_item()).buttons == ()


def test_at_most_5_buttons_counted_after_dropping() -> None:
    specs = (ButtonSpec("bad", "nope"),) + tuple(
        ButtonSpec(f"b{i}", "https://example.com") for i in range(8)
    )
    buttons = render_item(make_feed(buttons=specs), make_item()).buttons
    assert [b.label for b in buttons] == ["b0", "b1", "b2", "b3", "b4"]


# --- render_default ---


def customised_feed(**changes: Any) -> Feed:
    return make_feed(
        text_template="custom {{title}}",
        embed=EmbedSpec(title="{{title}}", image="{{image}}"),
        buttons=(ButtonSpec("Read", "{{link}}"),),
        mention_role_ids=(5, 6),
        post_as=PostAs.CUSTOM,
        custom_name="Newsie",
        custom_avatar="https://example.com/a.png",
        **changes,
    )


def test_render_default_ignores_template_embed_and_buttons() -> None:
    message = render_default(customised_feed(), make_item())
    assert message == OutgoingMessage(
        content="<@&5> <@&6> 📰 | **Hello world**\nhttps://example.com/hello",
        embed=None,
        buttons=(),
        mention_role_ids=(5, 6),
        username="Newsie",
        avatar_url="https://example.com/a.png",
    )


def test_render_default_keeps_forum_parts() -> None:
    feed = customised_feed(
        channel_kind=ChannelKind.FORUM,
        forum_title_template="Post: {{title}}",
        forum_tag_ids=(1, 2),
        forum_cover=True,
    )
    message = render_default(feed, make_item())
    assert message.thread_title == "Post: Hello world"
    assert message.tag_ids == (1, 2)
    assert message.cover_image_url == IMG
    assert message.embed is None
    assert message.buttons == ()


def test_render_item_uses_the_customisation() -> None:
    message = render_item(customised_feed(), make_item())
    assert message.content == "<@&5> <@&6> custom Hello world"
    assert message.embed == EmbedSpec(title="Hello world", image=IMG)
    assert message.buttons == (ButtonSpec("Read", "https://example.com/hello"),)


# --- Post as ---


def test_post_as_bot_leaves_name_and_avatar_unset() -> None:
    feed = make_feed(
        post_as=PostAs.BOT,
        custom_name="X",
        custom_avatar=IMG,
        site_name="Y",
        site_icon=IMG,
    )
    for fn in BOTH:
        message = fn(feed, make_item())
        assert message.username is None
        assert message.avatar_url is None


def test_post_as_site() -> None:
    feed = make_feed(post_as=PostAs.SITE, site_name="The Site", site_icon=IMG)
    message = render_item(feed, make_item())
    assert (message.username, message.avatar_url) == ("The Site", IMG)


def test_post_as_site_name_fallbacks() -> None:
    feed = make_feed(post_as=PostAs.SITE)
    assert render_item(feed, make_item()).username == "Example Blog"
    assert render_item(feed, make_item()).avatar_url is None
    feed = make_feed(post_as=PostAs.SITE, source_title="")
    assert render_item(feed, make_item()).username == "My Feed"


def test_post_as_custom() -> None:
    feed = make_feed(post_as=PostAs.CUSTOM, custom_name="Newsie", custom_avatar=IMG)
    message = render_item(feed, make_item())
    assert (message.username, message.avatar_url) == ("Newsie", IMG)
    feed = make_feed(post_as=PostAs.CUSTOM, site_name="Ignored")
    message = render_item(feed, make_item())
    assert (message.username, message.avatar_url) == ("My Feed", None)


def test_username_is_cut_to_80() -> None:
    feed = make_feed(post_as=PostAs.CUSTOM, custom_name="n" * 200)
    assert render_item(feed, make_item()).username == "n" * 79 + "…"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Discord Blog", f"D{ZWSP}iscord Blog"),
        ("the DISCORD and discord", f"the D{ZWSP}ISCORD and d{ZWSP}iscord"),
        ("Clyde's news", f"C{ZWSP}lyde's news"),
        ("discordclyde", f"d{ZWSP}iscordc{ZWSP}lyde"),
        ("everyone", f"e{ZWSP}veryone"),
        ("Here", f"H{ZWSP}ere"),
        ("two\nlines", "two lines"),
    ],
)
def test_usernames_discord_refuses_are_broken_up(name: str, expected: str) -> None:
    feed = make_feed(post_as=PostAs.CUSTOM, custom_name=name)
    assert render_item(feed, make_item()).username == expected


def test_long_username_with_refused_word_stays_within_80() -> None:
    feed = make_feed(post_as=PostAs.CUSTOM, custom_name="discord" * 30)
    username = render_item(feed, make_item()).username
    assert username is not None
    assert len(username) == 80
    assert "discord" not in username.lower()


def test_empty_username_falls_back_to_feed_name_then_none() -> None:
    feed = make_feed(post_as=PostAs.CUSTOM, custom_name="   ", name="Discord feed")
    assert render_item(feed, make_item()).username == f"D{ZWSP}iscord feed"
    feed = make_feed(post_as=PostAs.SITE, name="", source_title="", site_icon=IMG)
    message = render_item(feed, make_item())
    assert message.username is None
    assert message.avatar_url == IMG


@pytest.mark.parametrize("bad", ["", "data:image/png;base64,AAAA", "/icon.png", "ftp://x.example/i"])
def test_avatar_must_be_an_http_url(bad: str) -> None:
    feed = make_feed(post_as=PostAs.SITE, site_name="S", site_icon=bad)
    assert render_item(feed, make_item()).avatar_url is None
    feed = make_feed(post_as=PostAs.CUSTOM, custom_name="C", custom_avatar=bad)
    assert render_item(feed, make_item()).avatar_url is None


# --- Forum ---


def test_non_forum_feed_leaves_forum_parts_at_defaults() -> None:
    feed = make_feed(forum_title_template="{{title}}", forum_tag_ids=(1, 2), forum_cover=True)
    for fn in BOTH:
        message = fn(feed, make_item())
        assert message.thread_title is None
        assert message.tag_ids == ()
        assert message.cover_image_url is None


def test_forum_title_tags_and_no_cover_by_default() -> None:
    feed = make_feed(channel_kind=ChannelKind.FORUM, forum_tag_ids=(1, 2, 3))
    message = render_item(feed, make_item())
    assert message.thread_title == "Hello world"
    assert message.tag_ids == (1, 2, 3)
    assert message.cover_image_url is None


def test_forum_title_is_a_single_line_cut_to_100() -> None:
    feed = make_feed(channel_kind=ChannelKind.FORUM, forum_title_template="{{title}}\n{{author}}")
    assert render_item(feed, make_item(title="a\r\nb")).thread_title == "a b Ada"
    message = render_item(feed, make_item(title="t" * 300))
    assert message.thread_title == "t" * 99 + "…"


def test_forum_title_fallbacks() -> None:
    feed = make_feed(channel_kind=ChannelKind.FORUM, forum_title_template="{{author}}")
    assert render_item(feed, make_item(author="")).thread_title == "Hello world"
    assert render_item(feed, make_item(author="", title=" \n ")).thread_title == "My Feed"
    feed = make_feed(channel_kind=ChannelKind.FORUM, forum_title_template="", name="")
    assert render_item(feed, EMPTY_ITEM).thread_title == "New item"


def test_default_forum_title_falls_back_to_the_feed_title() -> None:
    feed = make_feed(channel_kind=ChannelKind.FORUM)
    assert render_item(feed, make_item(title="")).thread_title == "Example Blog"


def test_at_most_5_forum_tags() -> None:
    feed = make_feed(channel_kind=ChannelKind.FORUM, forum_tag_ids=(1, 2, 3, 4, 5, 6, 7))
    assert render_item(feed, make_item()).tag_ids == (1, 2, 3, 4, 5)


def test_cover_image() -> None:
    feed = make_feed(channel_kind=ChannelKind.FORUM, forum_cover=True)
    assert render_item(feed, make_item()).cover_image_url == IMG
    assert render_item(feed, make_item(image="")).cover_image_url is None
    assert render_item(feed, make_item(image="data:image/png;base64,AA")).cover_image_url is None


def test_cover_image_clears_the_same_embed_image() -> None:
    feed = make_feed(
        channel_kind=ChannelKind.FORUM,
        forum_cover=True,
        embed=EmbedSpec(title="{{title}}", image="{{image}}"),
    )
    message = render_item(feed, make_item())
    assert message.cover_image_url == IMG
    assert message.embed == EmbedSpec(title="Hello world")


def test_cover_image_leaves_a_different_embed_image() -> None:
    other = "https://example.com/other.png"
    feed = make_feed(
        channel_kind=ChannelKind.FORUM,
        forum_cover=True,
        embed=EmbedSpec(title="{{title}}", image=other),
    )
    message = render_item(feed, make_item())
    assert message.cover_image_url == IMG
    assert message.embed == EmbedSpec(title="Hello world", image=other)


def test_embed_image_is_kept_when_cover_is_off() -> None:
    feed = make_feed(channel_kind=ChannelKind.FORUM, embed=EmbedSpec(image="{{image}}"))
    message = render_item(feed, make_item())
    assert message.cover_image_url is None
    assert message.embed == EmbedSpec(image=IMG)


def test_embed_left_empty_by_the_cover_image_is_removed() -> None:
    feed = make_feed(
        channel_kind=ChannelKind.FORUM,
        forum_cover=True,
        text_template="",
        embed=EmbedSpec(image="{{image}}"),
    )
    message = render_item(feed, make_item())
    assert message.embed is None
    assert message.cover_image_url == IMG
    assert message.content == "📰 | **Hello world**\nhttps://example.com/hello"


# --- everything at once ---


@pytest.mark.parametrize("fn", BOTH)
@pytest.mark.parametrize("kind", list(ChannelKind))
@pytest.mark.parametrize("post_as", list(PostAs))
def test_enormous_fields_stay_within_every_limit(fn: Any, kind: Any, post_as: Any) -> None:
    huge = "@everyone discord clyde @here\n" * 2000
    huge_url = "https://example.com/" + "a" * 5000
    feed = make_feed(
        channel_kind=kind,
        name=huge,
        source_title=huge,
        text_template="{{mentions}} {{title}} {{content}} {{summary}}" * 3,
        embed=EmbedSpec(
            title="{{title}}",
            description="{{content}}",
            url="{{link}}",
            image="{{image}}",
            footer="{{summary}}",
            colour=2**40,
            fields=tuple(FieldSpec("{{title}}", "{{content}}") for _ in range(60)),
        ),
        buttons=tuple(ButtonSpec("{{title}}", u) for u in ("{{link}}", "https://ok.example") * 6),
        mention_role_ids=tuple(range(10**17, 10**17 + 20)),
        post_as=post_as,
        custom_name=huge,
        custom_avatar=huge_url,
        site_name=huge,
        site_icon=huge_url,
        forum_title_template="{{title}}\n{{content}}",
        forum_tag_ids=tuple(range(20)),
        forum_cover=True,
    )
    item = make_item(
        title=huge,
        link=huge_url,
        summary=huge,
        content=huge,
        author=huge,
        categories=(huge, huge),
        image=huge_url,
    )
    message = fn(feed, item)
    assert_acceptable(message)
    assert message.mention_role_ids == feed.mention_role_ids
    if fn is render_default:
        assert message.embed is None
        assert message.buttons == ()
    else:
        assert message.embed is not None
        assert len(message.buttons) == 5


@pytest.mark.parametrize("fn", BOTH)
def test_unusable_placeholders_never_raise(fn: Any) -> None:
    feed = make_feed(
        channel_kind=ChannelKind.FORUM,
        text_template="{{nope}} {{title:0}} {{ {{",
        embed=EmbedSpec(title="{{}}", description="{{title:abc}}", fields=(FieldSpec("{{", "}}"),)),
        buttons=(ButtonSpec("{{x}}", "{{y}}"),),
        forum_title_template="{{bad}}",
    )
    assert_acceptable(fn(feed, make_item()))
    assert_acceptable(fn(feed, EMPTY_ITEM))


# --- long values are shortened, not the end of the Template ---


def test_a_long_value_is_shortened_so_the_rest_of_the_text_survives() -> None:
    feed = make_feed(
        text_template="{{description}}\n{{link}}\n{{mentions}}", mention_role_ids=(555,)
    )
    message = render_item(feed, make_item(summary="x" * 3000))
    tail = "\nhttps://example.com/hello\n<@&555>"
    assert message.content == "x" * (1999 - len(tail)) + "…" + tail
    assert len(message.content) == 2000


def test_the_longest_values_are_shortened_first() -> None:
    feed = make_feed(text_template="{{title}}: {{summary}} / {{content}} by {{author}}")
    item = make_item(title="Short", summary="s" * 3000, content="c" * 1200)
    room = 2000 - len("Short:  /  by Ada")
    assert render_item(feed, item).content == (
        f"Short: {'s' * (room - 992)}… / {'c' * 990}… by Ada"
    )


def test_a_placeholder_with_its_own_limit_keeps_it() -> None:
    feed = make_feed(text_template="{{summary:300}}|{{content}}|{{link}}")
    message = render_item(feed, make_item(summary="s" * 3000, content="c" * 3000))
    tail = "|https://example.com/hello"
    assert message.content == "s" * 299 + "…|" + "c" * (2000 - 301 - len(tail) - 1) + "…" + tail


def test_mentions_are_never_the_value_that_is_shortened() -> None:
    roles = tuple(range(1000, 1040))
    feed = make_feed(text_template="{{mentions}}\n{{title}}", mention_role_ids=roles)
    mentions = " ".join(f"<@&{role}>" for role in roles)
    message = render_item(feed, make_item(title="t" * 3000))
    assert message.content == mentions + "\n" + "t" * (1998 - len(mentions)) + "…"


def test_template_text_that_is_too_long_by_itself_is_still_cut() -> None:
    feed = make_feed(text_template="y" * 1999 + " {{title}} end")
    message = render_item(feed, make_item())
    assert message.content == "y" * 1999 + "…"


def test_a_long_value_in_the_embed_description_is_shortened_not_the_tail() -> None:
    feed = make_feed(embed=EmbedSpec(description="{{content}}\n[Read more]({{link}})"))
    embed = render_item(feed, make_item(content="c" * 5000)).embed
    assert embed is not None
    tail = "\n[Read more](https://example.com/hello)"
    assert embed.description == "c" * (4095 - len(tail)) + "…" + tail


# --- a cut never leaves a code block open ---


def test_cutting_inside_a_code_block_closes_it() -> None:
    feed = make_feed(text_template="{{content}}\n{{link}}")
    message = render_item(feed, make_item(content="Look:\n```py\n" + "x = 1\n" * 1000 + "```"))
    assert len(message.content) <= 2000
    assert message.content.count("```") == 2
    assert message.content.endswith("…\n```\nhttps://example.com/hello")


def test_cutting_literal_template_text_inside_a_code_block_closes_it() -> None:
    feed = make_feed(text_template="```\n" + "y" * 1991 + "{{title:50}}{{title:50}}\n```")
    message = render_item(feed, make_item(title="t" * 50))
    assert len(message.content) == 2000
    assert message.content.endswith("y…\n```")
    assert message.content.count("```") == 2


def test_a_cut_that_removes_the_opening_fence_adds_no_closing_one() -> None:
    feed = make_feed(text_template="{{content}}")
    message = render_item(feed, make_item(content="x" * 1996 + "```\ncode\n```"))
    assert message.content == "x" * 1995 + "…"


def test_a_cut_after_a_closed_code_block_adds_nothing() -> None:
    feed = make_feed(text_template="{{content}}")
    message = render_item(feed, make_item(content="```\ncode\n```\n" + "x" * 3000))
    assert message.content == "```\ncode\n```\n" + "x" * 1986 + "…"


def test_cutting_the_embed_description_inside_a_code_block_closes_it() -> None:
    feed = make_feed(embed=EmbedSpec(description="{{content}}"))
    embed = render_item(feed, make_item(content="```\n" + "x" * 5000 + "\n```")).embed
    assert embed is not None
    assert embed.description == "```\n" + "x" * (4096 - 4 - 5) + "…\n```"


# --- Placeholders inside a web address ---


def test_a_text_value_inside_an_address_is_percent_encoded() -> None:
    feed = make_feed(
        embed=EmbedSpec(
            title="T",
            url="https://google.com/search?q={{title}}",
            image="https://img.example/render?text={{title}}",
        ),
        buttons=(ButtonSpec("Search", "https://google.com/search?q={{title}}"),),
    )
    message = render_item(feed, make_item(title="Hello World & more"))
    assert message.buttons == (
        ButtonSpec("Search", "https://google.com/search?q=Hello%20World%20%26%20more"),
    )
    assert message.embed is not None
    assert message.embed.url == "https://google.com/search?q=Hello%20World%20%26%20more"
    assert message.embed.image == "https://img.example/render?text=Hello%20World%20%26%20more"


def test_an_address_placeholder_used_as_the_address_is_left_as_it_is() -> None:
    link = "https://example.com/a?b=1&c=%20d#e"
    feed = make_feed(
        embed=EmbedSpec(title="T", url="{{link}}", image="{{image}}"),
        buttons=(
            ButtonSpec("Read", "{{link}}"),
            ButtonSpec("Tracked", "{{link}}&utm={{feed_title}}"),
            ButtonSpec("Site", "{{feed_link}}/about"),
        ),
    )
    message = render_item(feed, make_item(link=link, image=IMG + "?w=1&h=2"))
    assert message.embed is not None
    assert (message.embed.url, message.embed.image) == (link, IMG + "?w=1&h=2")
    assert [b.url for b in message.buttons] == [
        link,
        link + "&utm=Example%20Blog",
        "https://example.com/about",
    ]


def test_an_address_in_the_query_of_another_is_encoded_but_not_in_its_path() -> None:
    feed = make_feed(
        buttons=(
            ButtonSpec("Share", "https://share.example/?u={{link}}&t={{title:5}}"),
            ButtonSpec("Archive", "https://web.archive.org/web/{{link}}"),
        )
    )
    message = render_item(feed, make_item(link="https://example.com/a?b=1"))
    assert [b.url for b in message.buttons] == [
        "https://share.example/?u=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&t=Hell%E2%80%A6",
        "https://web.archive.org/web/https://example.com/a?b=1",
    ]


# --- places where Discord shows timestamps and mentions as raw text ---


def test_date_and_mentions_are_plain_where_discord_does_not_interpret_them() -> None:
    feed = make_feed(
        channel_kind=ChannelKind.FORUM,
        forum_title_template="{{date}} {{title}}",
        mention_role_ids=(7,),
        embed=EmbedSpec(
            title="{{mentions}} {{title}} ({{date}})",
            description="{{date}} {{mentions}}",
            footer="{{date}}{{mentions}}",
            fields=(FieldSpec("{{date||title}}", "{{date}} {{mentions}}"),),
        ),
        buttons=(ButtonSpec("{{date}} {{mentions}}", "{{link}}"),),
    )
    message = render_item(feed, make_item())
    plain = "14 Nov 2023 22:13 UTC"
    assert message.embed == EmbedSpec(
        title=f"Hello world ({plain})",
        description="<t:1700000000:f> <@&7>",
        footer=plain,
        fields=(FieldSpec(plain, "<t:1700000000:f> <@&7>"),),
    )
    assert message.buttons == (ButtonSpec(plain, "https://example.com/hello"),)
    assert message.thread_title == f"{plain} Hello world"


def test_plain_date_of_an_item_without_one_is_empty_and_falls_back() -> None:
    feed = make_feed(
        embed=EmbedSpec(title="T", footer="{{date||author}}"),
        buttons=(ButtonSpec("{{date}}", "{{link}}"),),
    )
    message = render_item(feed, make_item(published=None))
    assert message.embed is not None
    assert message.embed.footer == "Ada"
    assert message.buttons == ()


def test_a_date_out_of_range_never_raises() -> None:
    feed = make_feed(embed=EmbedSpec(title="T", footer="{{date}}"))
    message = render_item(feed, make_item(published=10**18))
    assert message.embed is not None
    assert message.embed.footer == ""


# --- which Buttons are left out ---


def test_hidden_buttons_names_the_positions_left_out_for_this_item() -> None:
    feed = make_feed(
        buttons=(
            ButtonSpec("Read", "{{link}}"),
            ButtonSpec("Picture", "{{image}}"),
            ButtonSpec("{{author}}", "https://example.com/"),
        )
    )
    assert hidden_buttons(feed, make_item()) == ()
    assert hidden_buttons(feed, make_item(image="", author="")) == (2, 3)
    assert hidden_buttons(make_feed(), make_item()) == ()


def test_message_carries_the_items_date_and_the_embed_setting() -> None:
    feed = make_feed(embed=EmbedSpec(title="{{title}}", timestamp=False))
    message = render_item(feed, make_item(published=1_700_000_000))
    assert message.published == 1_700_000_000
    assert message.embed is not None and message.embed.timestamp is False
