from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from cue_notify.core import templating
from cue_notify.core.content import LocalizedTemplate


def test_render_and_number_filter() -> None:
    rendered = templating.render(
        "Paid {{ data.amount | number }} so'm ({{ data.rate | number(2, ',', '.') }}%)",
        {"data": {"amount": 1250000, "rate": "12.5"}},
    )
    assert rendered == "Paid 1 250 000 so'm (12.50%)"
    assert templating.render("{{ -1234.5 | number(1) }}", {}) == "-1 234.5"


def test_undefined_variables_raise() -> None:
    with pytest.raises(templating.RenderError):
        templating.render("Hi {{ data.name }}", {"data": {}})


def test_sandbox_blocks_python_internals() -> None:
    with pytest.raises(templating.RenderError):
        templating.render("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})
    with pytest.raises(templating.RenderError):
        templating.render("{{ data.update({'x': 1}) }}", {"data": {}})  # immutable sandbox


def test_html_is_escaped_but_text_is_not() -> None:
    context = {"data": {"name": "<b>Ann</b>"}}
    assert templating.render("Hi {{ data.name }}", context) == "Hi <b>Ann</b>"
    assert (
        templating.render("Hi {{ data.name }}", context, html=True) == "Hi &lt;b&gt;Ann&lt;/b&gt;"
    )


def test_syntax_errors_are_caught_at_validation_time() -> None:
    with pytest.raises(ValidationError, match="body"):
        LocalizedTemplate(body="Hello {{ name")


def test_channel_overrides_inherit_unset_fields() -> None:
    template = LocalizedTemplate.model_validate(
        {
            "title": "Hi {{ data.n }}",
            "body": "Long body for {{ data.n }}",
            "url": "https://x.test/{{ data.n }}",
            "data": {"screen": "home"},
            "channels": {"sms": {"body": "Short {{ data.n }}"}},
        }
    )
    variants = template.render_variants({"data": {"n": 7}})
    assert variants["default"].body == "Long body for 7"
    assert variants["sms"].body == "Short 7"
    assert variants["sms"].title == "Hi 7"
    assert variants["sms"].url == "https://x.test/7"
    assert variants["sms"].data == {"screen": "home"}


@pytest.mark.parametrize(
    "source",
    [
        "{{ (9**99999999) > 1 }}",
        "{{ 'a' * 300000000 }}",
        "{{ [0] * 300000000 }}",
        "{% for i in range(100000) %}x{% endfor %}",
        "{% for i in range(999) %}{% for j in range(999) %}xxxxx{% endfor %}{% endfor %}",
        "{{ '%1000000000d' % 1 }}",
        "{{ '{:>999999999}'.format(1) }}",
        "{{ 'x'.ljust(1000000000) }}",
        "{{ 'x' | center(1000000000) }}",
        "{{ 'x' | indent(1000000000) }}",
        "{{ '%s' | format(1) }}",
    ],
)
def test_resource_exhaustion_is_blocked_quickly(source: str) -> None:
    started = time.perf_counter()
    with pytest.raises((templating.RenderError, ValidationError)):
        LocalizedTemplate(body=source).render({})
    assert time.perf_counter() - started < 1


def test_bounded_operations_still_work() -> None:
    rendered = templating.render(
        "{{ 2 ** 10 }} {{ 'ab' * 2 }} {{ 7 % 3 }} {{ 'x' | center(5) }}|"
        "{% for i in range(3) %}{{ i }}{% endfor %}",
        {},
    )
    assert rendered == "1024 abab 1   x  |012"
