from __future__ import annotations

import pytest

from rssbot.models import (
    DEFAULT_FORUM_TITLE_TEMPLATE,
    DEFAULT_INTERVAL_S,
    DEFAULT_TEXT_TEMPLATE,
    ChannelKind,
    Feed,
    Item,
    PostAs,
)
from rssbot.template import (
    ADDRESS_PLACEHOLDERS,
    ALIASES,
    PLACEHOLDERS,
    TemplateError,
    Use,
    leading_names,
    render,
    uses,
    validate,
    values_for,
)

VALUES = {
    "title": "Hello world",
    "link": "https://example.com/a",
    "description": "A long description of the item",
    "summary": "",
    "content": "Full content",
    "author": "Ada",
    "date": "<t:1700000000:f>",
    "categories": "news, tech",
    "image": "",
    "feed_title": "Example Feed",
    "feed_link": "https://example.com",
    "mentions": "<@&1> <@&2>",
}


def make_item(**changes: object) -> Item:
    fields: dict[str, object] = {
        "key": "k1",
        "title": "Hello world",
        "link": "https://example.com/a",
        "summary": "Short",
        "content": "Full content",
        "author": "Ada",
        "published": 1700000000,
        "categories": ("news", "tech"),
        "image": "https://example.com/a.png",
    }
    fields.update(changes)
    return Item(**fields)  # type: ignore[arg-type]


def make_feed(**changes: object) -> Feed:
    fields: dict[str, object] = {
        "id": 1,
        "server_id": 10,
        "channel_id": 20,
        "channel_kind": ChannelKind.MESSAGES,
        "name": "My feed",
        "url": "https://example.com/rss",
        "interval_s": DEFAULT_INTERVAL_S,
        "source_title": "Example Feed",
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
    fields.update(changes)
    return Feed(**fields)  # type: ignore[arg-type]


# --- names ---


def test_placeholder_names_and_aliases() -> None:
    assert PLACEHOLDERS == (
        "title",
        "link",
        "description",
        "summary",
        "content",
        "author",
        "date",
        "categories",
        "image",
        "feed_title",
        "feed_link",
        "mentions",
    )
    assert ALIASES == {"url": "link"}
    assert all(target in PLACEHOLDERS for target in ALIASES.values())


@pytest.mark.parametrize("name", PLACEHOLDERS)
def test_every_name_renders_its_value(name: str) -> None:
    values = {n: f"<{n}>" for n in PLACEHOLDERS}
    assert render("{{" + name + "}}", values) == f"<{name}>"
    validate("{{" + name + "}}")


# --- simple placeholders ---


def test_plain_text_is_unchanged() -> None:
    assert render("no placeholders here", VALUES) == "no placeholders here"
    assert render("", VALUES) == ""


def test_simple_placeholder() -> None:
    assert render("**{{title}}**", VALUES) == "**Hello world**"


@pytest.mark.parametrize(
    "template",
    ["{{TITLE}}", "{{Title}}", "{{ title }}", "{{  tItLe\t}}", "{{title }}", "{{ title}}"],
)
def test_case_and_spacing_are_ignored(template: str) -> None:
    validate(template)
    assert render(template, VALUES) == "Hello world"


@pytest.mark.parametrize("template", ["{{url}}", "{{URL}}", "{{ Url }}"])
def test_url_alias_means_link(template: str) -> None:
    validate(template)
    assert render(template, VALUES) == "https://example.com/a"


def test_empty_value_renders_as_nothing() -> None:
    assert render("[{{image}}]", VALUES) == "[]"


def test_value_is_inserted_with_its_own_whitespace() -> None:
    assert render("[{{title}}]", {"title": "  padded  "}) == "[  padded  ]"


def test_missing_value_renders_as_nothing() -> None:
    assert render("[{{title}}]", {}) == "[]"


def test_non_string_values_do_not_raise() -> None:
    assert render("{{title}}|{{link}}", {"title": None, "link": 42}) == "|42"  # type: ignore[dict-item]


def test_default_templates() -> None:
    validate(DEFAULT_TEXT_TEMPLATE)
    validate(DEFAULT_FORUM_TITLE_TEMPLATE)
    assert render(DEFAULT_TEXT_TEMPLATE, VALUES) == "📰 | **Hello world**\nhttps://example.com/a"
    assert render(DEFAULT_FORUM_TITLE_TEMPLATE, {**VALUES, "title": ""}) == "Example Feed"


def test_multi_line_template() -> None:
    template = "# {{title}}\n\n{{description:7}}\n{{link}}\n"
    assert render(template, VALUES) == "# Hello world\n\nA long…\nhttps://example.com/a\n"


def test_emoji_in_template_and_value() -> None:
    assert render("🔥 {{title}} 🔥", {"title": "📰 news"}) == "🔥 📰 news 🔥"


def test_adjacent_placeholders() -> None:
    assert render("{{author}}{{title}}", VALUES) == "AdaHello world"
    assert render("{{author}}{{image}}{{author}}", VALUES) == "AdaAda"


def test_same_placeholder_twice() -> None:
    assert render("{{author}} and {{author}}", VALUES) == "Ada and Ada"


# --- Fallback ---


def test_fallback_uses_first_non_empty() -> None:
    assert render("{{summary||description}}", VALUES) == "A long description of the item"
    assert render("{{author||title}}", VALUES) == "Ada"


def test_fallback_with_many_alternatives() -> None:
    assert render("{{summary||image||author||title}}", VALUES) == "Ada"


def test_fallback_skips_whitespace_only_values() -> None:
    values = {"summary": " \n\t ", "content": "", "title": "T"}
    assert render("{{summary||content||title}}", values) == "T"


def test_fallback_all_empty() -> None:
    assert render("[{{summary||image}}]", VALUES) == "[]"
    assert render("[{{summary||image}}]", {"summary": "  ", "image": "\n"}) == "[]"


def test_fallback_case_spacing_and_alias() -> None:
    template = "{{ Summary || IMAGE || url }}"
    validate(template)
    assert render(template, VALUES) == "https://example.com/a"


def test_fallback_with_the_same_name_twice() -> None:
    assert render("{{title||title}}", VALUES) == "Hello world"


# --- length limit ---


def test_limit_shorter_than_value() -> None:
    assert render("{{title:8}}", VALUES) == "Hello w…"
    assert len(render("{{title:8}}", VALUES)) == 8


def test_limit_equal_to_value() -> None:
    assert render("{{title:11}}", VALUES) == "Hello world"


def test_limit_one_less_than_value() -> None:
    assert render("{{title:10}}", VALUES) == "Hello wor…"


def test_limit_longer_than_value() -> None:
    assert render("{{title:200}}", VALUES) == "Hello world"


def test_limit_of_one() -> None:
    assert render("{{title:1}}", VALUES) == "…"
    assert render("{{title:1}}", {"title": "x"}) == "x"
    assert render("{{title:1}}", {"title": ""}) == ""


def test_limit_leaves_no_whitespace_before_the_ellipsis() -> None:
    assert render("{{title:7}}", VALUES) == "Hello…"
    assert render("{{title:6}}", VALUES) == "Hello…"
    assert render("{{title:5}}", {"title": "a  \n  b"}) == "a…"


def test_limit_counts_characters() -> None:
    assert render("{{title:3}}", {"title": "📰📰📰📰"}) == "📰📰…"
    assert render("{{title:4}}", {"title": "📰📰📰📰"}) == "📰📰📰📰"
    assert render("{{title:3}}", {"title": "éèêë"}) == "éè…"


def test_limit_applies_to_the_whole_fallback() -> None:
    template = "{{summary||description:7}}"
    validate(template)
    assert render(template, VALUES) == "A long…"
    assert render(template, {"summary": "Short text", "description": "x"}) == "Short…"
    assert render(template, {"summary": " ", "description": "abcdefgh"}) == "abcdef…"


def test_limit_with_spaces_and_leading_zeros() -> None:
    for template in ("{{ title : 8 }}", "{{title:008}}", "{{TITLE:8}}"):
        validate(template)
        assert render(template, VALUES) == "Hello w…"


def test_enormous_limit_is_valid_and_does_not_raise() -> None:
    template = "{{title:" + "9" * 6000 + "}}"
    validate(template)
    assert render(template, VALUES) == "Hello world"


def test_result_never_exceeds_the_limit() -> None:
    value = "word " * 50
    for limit in range(1, 60):
        out = render("{{title:" + str(limit) + "}}", {"title": value})
        assert 1 <= len(out) <= limit
        assert out.endswith("…")
        assert not out[:-1].endswith(" ")


# --- literal braces ---


@pytest.mark.parametrize(
    "template",
    [
        "{",
        "}",
        "{}",
        "{title}",
        "{{",
        "}}",
        "{{title",
        "title}}",
        "{{title}",
        "{title}}",
        "}}title{{",
        "a { b } c",
        "{{{",
        "}}}}",
        "{ {title} }",
        'json: {"a": {"b": 1}}',
    ],
)
def test_stray_and_unbalanced_braces_are_literal(template: str) -> None:
    validate(template)
    assert render(template, VALUES) == template
    assert not uses(template, "title")


def test_unclosed_braces_before_a_placeholder_stay_literal() -> None:
    assert render("{{ oops {{title}}", VALUES) == "{{ oops Hello world"
    assert render("{{title}} and {{link", VALUES) == "Hello world and {{link"
    validate("{{ oops {{title}}")


def test_three_braces_in_a_row() -> None:
    validate("{{{title}}}")
    assert render("{{{title}}}", VALUES) == "{Hello world}"
    assert render("{{{title}}", VALUES) == "{Hello world"
    assert render("{{title}}}", VALUES) == "Hello world}"
    assert uses("{{{title}}}", "title")


def test_four_and_more_braces_in_a_row() -> None:
    assert render("{{{{title}}}}", VALUES) == "{{Hello world}}"
    assert render("{{{{{title}}}}}", VALUES) == "{{{Hello world}}}"


def test_single_braces_around_and_between_placeholders() -> None:
    assert render("{{{author}}{{title}}}", VALUES) == "{AdaHello world}"
    assert render("{ {{author}} }", VALUES) == "{ Ada }"


# --- inserted values are not scanned again ---


def test_value_containing_a_placeholder_stays_literal() -> None:
    values = {"feed_title": "{{link}}", "link": "https://example.com/a"}
    assert render("{{feed_title}}", values) == "{{link}}"
    assert render("{{feed_title}} {{link}}", values) == "{{link}} https://example.com/a"


def test_values_cannot_join_up_with_template_braces() -> None:
    values = {"title": "{{", "author": "link", "link": "L"}
    assert render("{{title}}{{author}}}}", values) == "{{link}}"
    assert render("{{{title}}link}}", {"title": "{", "link": "L"}) == "{{link}}"


def test_values_containing_braces_and_backslashes() -> None:
    assert render("{{title}}", {"title": "a { b } c }} {{"}) == "a { b } c }} {{"
    assert render("{{title}}", {"title": r"\1 \g<0> \n"}) == r"\1 \g<0> \n"


# --- invalid placeholders ---


@pytest.mark.parametrize(
    "template",
    [
        "{{nope}}",
        "{{}}",
        "{{ }}",
        "{{title||}}",
        "{{||title}}",
        "{{title||||link}}",
        "{{title||nope}}",
        "{{title|link}}",
        "{{title|||link}}",
        "{{ti tle}}",
        "{{title:0}}",
        "{{title:00}}",
        "{{title:-5}}",
        "{{title:+5}}",
        "{{title:abc}}",
        "{{title:1.5}}",
        "{{title:}}",
        "{{title: }}",
        "{{title:5:6}}",
        "{{title:5||link}}",
        "{{title:٣}}",
        "{{:5}}",
        "{{title\nlink}}",
    ],
)
def test_invalid_placeholder_fails_validation_and_renders_as_nothing(template: str) -> None:
    with pytest.raises(TemplateError):
        validate(template)
    assert render("[" + template + "]", VALUES) == "[]"
    assert not uses(template, "title")


def test_invalid_placeholder_does_not_affect_the_rest() -> None:
    assert render("{{nope}}{{title}} {{title:0}}{{author}}", VALUES) == "Hello world Ada"


def test_template_error_is_a_value_error() -> None:
    assert issubclass(TemplateError, ValueError)


def test_unknown_name_message_lists_the_valid_names() -> None:
    with pytest.raises(TemplateError) as caught:
        validate("Hi {{ Tilte }}")
    message = str(caught.value)
    assert '"Tilte"' in message
    assert "{{Tilte}}" in message  # quoted without the surrounding spaces
    for name in PLACEHOLDERS:
        assert name in message
    assert message.endswith(".")
    assert "\n" not in message


def test_unknown_name_message_names_the_bad_alternative() -> None:
    with pytest.raises(TemplateError) as caught:
        validate("{{title||sumary||link}}")
    assert '"sumary"' in str(caught.value)
    assert "{{title||sumary||link}}" in str(caught.value)


def test_empty_name_message() -> None:
    with pytest.raises(TemplateError) as caught:
        validate("{{title||}}")
    assert str(caught.value) == "{{title||}} has an empty Placeholder name."
    with pytest.raises(TemplateError) as caught:
        validate("{{}}")
    assert str(caught.value) == "{{}} has an empty Placeholder name."


def test_bad_limit_message() -> None:
    with pytest.raises(TemplateError) as caught:
        validate("{{title:abc}}")
    assert str(caught.value) == (
        'The length limit "abc" in {{title:abc}} must be a whole number greater than 0.'
    )
    with pytest.raises(TemplateError) as caught:
        validate("{{title:0}}")
    assert '"0"' in str(caught.value)


def test_validate_reports_the_first_problem() -> None:
    with pytest.raises(TemplateError) as caught:
        validate("{{title}} {{first}} {{second}}")
    assert '"first"' in str(caught.value)
    assert "second" not in str(caught.value)


def test_message_shortens_long_and_multi_line_text() -> None:
    with pytest.raises(TemplateError) as caught:
        validate("{{" + "x" * 5000 + "\n" + "y" * 5000 + "}}")
    message = str(caught.value)
    assert len(message) < 400
    assert "\n" not in message


def test_validate_accepts_valid_templates() -> None:
    validate("")
    validate("plain")
    validate("{{title}} {{summary||description:200}} {{url}} { } {{")


# --- uses ---


def test_uses_simple() -> None:
    assert uses("**{{title}}**", "title")
    assert not uses("**{{title}}**", "link")
    assert not uses("title", "title")


def test_uses_counts_aliases_both_ways() -> None:
    assert uses("{{url}}", "link")
    assert uses("{{link}}", "url")
    assert uses("{{URL}}", "Link")


def test_uses_counts_fallback_alternatives_and_limits() -> None:
    template = "{{summary||description:200}}"
    assert uses(template, "summary")
    assert uses(template, "description")
    assert not uses(template, "content")


def test_uses_ignores_case_and_spacing() -> None:
    assert uses("{{ MENTIONS }}", "mentions")
    assert uses("{{mentions}}", " Mentions ")


def test_uses_ignores_invalid_placeholders() -> None:
    assert not uses("{{title||nope}}", "title")
    assert not uses("{{title:0}}", "title")
    assert uses("{{title||nope}} {{title}}", "title")


def test_uses_unknown_name_is_false() -> None:
    assert not uses("{{nope}}", "nope")
    assert not uses("{{title}}", "")


# --- values_for ---


def test_values_for_has_every_name() -> None:
    values = values_for(make_feed(), make_item())
    assert tuple(values) == PLACEHOLDERS
    assert all(isinstance(value, str) for value in values.values())


def test_values_for_item_fields() -> None:
    values = values_for(make_feed(mention_role_ids=(111, 222)), make_item())
    assert values == {
        "title": "Hello world",
        "link": "https://example.com/a",
        "description": "Short",
        "summary": "Short",
        "content": "Full content",
        "author": "Ada",
        "date": "<t:1700000000:f>",
        "categories": "news, tech",
        "image": "https://example.com/a.png",
        "feed_title": "Example Feed",
        "feed_link": "https://example.com",
        "mentions": "<@&111> <@&222>",
    }


def test_values_for_description_falls_back_to_content() -> None:
    values = values_for(make_feed(), make_item(summary=""))
    assert values["summary"] == ""
    assert values["description"] == "Full content"


def test_values_for_date() -> None:
    assert values_for(make_feed(), make_item(published=None))["date"] == ""
    assert values_for(make_feed(), make_item(published=0))["date"] == "<t:0:f>"


def test_values_for_categories() -> None:
    assert values_for(make_feed(), make_item(categories=()))["categories"] == ""
    assert values_for(make_feed(), make_item(categories=("one",)))["categories"] == "one"


def test_values_for_feed_title_falls_back_to_the_feed_name() -> None:
    assert values_for(make_feed(source_title=""), make_item())["feed_title"] == "My feed"


def test_values_for_mentions() -> None:
    assert values_for(make_feed(), make_item())["mentions"] == ""
    assert values_for(make_feed(mention_role_ids=(5,)), make_item())["mentions"] == "<@&5>"


def test_values_for_feeds_render() -> None:
    feed = make_feed(source_title="{{link}}", mention_role_ids=(7,))
    values = values_for(feed, make_item(title="", summary="x" * 300))
    out = render("{{mentions}} {{title||feed_title}}\n{{description:10}}\n{{date}}", values)
    assert out == "<@&7> {{link}}\n" + "x" * 9 + "…\n<t:1700000000:f>"


# --- render with a transform ---


def test_transform_sees_each_value_after_its_own_limit_with_where_it_stands() -> None:
    seen: list[tuple[str, Use]] = []

    def record(value: str, use: Use) -> str:
        seen.append((value, use))
        return value.upper()

    text = render(
        "a {{title:5}} b {{summary||content}} {{image}}{{nope}}", VALUES, transform=record
    )
    assert text == "a HELL… b FULL CONTENT "
    assert seen == [
        ("Hell…", Use(name="title", start=2, limited=True)),
        ("Full content", Use(name="content", start=16, limited=False)),
    ]


def test_render_without_a_transform_is_unchanged() -> None:
    assert render("{{title}}", VALUES) == render("{{title}}", VALUES, transform=None)


# --- leading_names ---


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{{link}}", ("link",)),
        ("{{ URL }}?a=1", ("link",)),
        ("{{image||link:200}}/x", ("image", "link")),
        ("{{title}}", ("title",)),
        ("https://e.com/{{link}}", ()),
        (" {{link}}", ()),
        ("{{nope}}", ()),
        ("{{{link}}}", ()),
        ("", ()),
    ],
)
def test_leading_names(template: str, expected: tuple[str, ...]) -> None:
    assert leading_names(template) == expected


def test_address_placeholders_are_placeholders() -> None:
    assert set(ADDRESS_PLACEHOLDERS) == {"link", "image", "feed_link"}
    assert set(ADDRESS_PLACEHOLDERS) <= set(PLACEHOLDERS)
