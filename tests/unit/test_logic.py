from __future__ import annotations

from typing import Any

import pytest

from cue_notify.core import logic

DATA = {
    "event": {"name": "order.paid", "data": {"total": 250, "items": [1, 2, 3], "code": "VIP-7"}},
    "recipient": {"locale": "uz", "attributes": {"tier": "gold", "tags": ["beta", "ios"]}},
}


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        # data access
        ({"var": "event.data.total"}, 250),
        ({"var": "event.data.items.1"}, 2),
        ({"var": ["event.data.missing", "fallback"]}, "fallback"),
        ({"var": ""}, DATA),
        ({"missing": ["event.data.total", "event.data.nope"]}, ["event.data.nope"]),
        ({"missing_some": [1, ["event.data.total", "event.data.nope"]]}, []),
        # comparisons & JavaScript-style coercion
        ({">": [{"var": "event.data.total"}, 100]}, True),
        ({"<=": [1, "1"]}, True),
        ({"<": [1, 5, 10]}, True),
        ({"<": [1, 15, 10]}, False),
        ({"==": [1, "1"]}, True),
        ({"===": [1, "1"]}, False),
        ({"==": [None, 0]}, False),
        ({"!=": ["a", "b"]}, True),
        # logic
        ({"and": [True, "x", 3]}, 3),
        ({"or": [False, 0, "y"]}, "y"),
        ({"!": [[]]}, True),
        ({"!!": ["0"]}, True),
        ({"if": [False, "a", True, "b", "c"]}, "b"),
        ({"if": [False, "a"]}, None),
        # arithmetic
        ({"+": [1, "2", 3.5]}, 6.5),
        ({"-": [5]}, -5),
        ({"*": [2, 3]}, 6),
        ({"/": [1, 0]}, None),
        ({"%": [7, 4]}, 3),
        ({"max": [1, 9, 3]}, 9),
        ({"min": []}, None),
        # arrays
        ({"in": ["beta", {"var": "recipient.attributes.tags"}]}, True),
        ({"in": ["VIP", {"var": "event.data.code"}]}, True),
        ({"map": [{"var": "event.data.items"}, {"*": [{"var": ""}, 2]}]}, [2, 4, 6]),
        ({"filter": [{"var": "event.data.items"}, {">": [{"var": ""}, 1]}]}, [2, 3]),
        (
            {
                "reduce": [
                    {"var": "event.data.items"},
                    {"+": [{"var": "current"}, {"var": "accumulator"}]},
                    0,
                ]
            },
            6,
        ),
        ({"all": [{"var": "event.data.items"}, {">": [{"var": ""}, 0]}]}, True),
        ({"all": [[], True]}, False),
        ({"some": [{"var": "event.data.items"}, {"==": [{"var": ""}, 3]}]}, True),
        ({"none": [{"var": "event.data.items"}, {"==": [{"var": ""}, 9]}]}, True),
        ({"merge": [[1], 2, [3, 4]]}, [1, 2, 3, 4]),
        # strings
        ({"cat": ["Hi ", {"var": "recipient.locale"}, 1.0]}, "Hi uz1"),
        ({"substr": ["abcdef", -2]}, "ef"),
        ({"substr": ["abcdef", 1, -1]}, "bcde"),
        ({"starts_with": [{"var": "event.name"}, "order."]}, True),
        ({"lower": ["ABC"]}, "abc"),
    ],
)
def test_operators(rule: Any, expected: Any) -> None:
    assert logic.evaluate(rule, DATA) == expected


def test_matches_none_is_always_true() -> None:
    assert logic.matches(None, {})


def test_unknown_operator_is_rejected_statically_and_at_runtime() -> None:
    rule = {"and": [{"exec": ["os.system"]}]}
    with pytest.raises(logic.LogicError, match="unknown operator 'exec'"):
        logic.validate(rule)
    with pytest.raises(logic.LogicError):
        logic.evaluate(rule, {})


def test_excessive_nesting_is_rejected() -> None:
    rule: Any = True
    for _ in range(logic.MAX_DEPTH + 5):
        rule = {"!": [rule]}
    with pytest.raises(logic.LogicError, match="nesting"):
        logic.validate(rule)
    with pytest.raises(logic.LogicError, match="nesting"):
        logic.evaluate(rule, {})


def test_wrong_arity_is_a_logic_error() -> None:
    with pytest.raises(logic.LogicError, match="invalid arguments"):
        logic.evaluate({"==": [1]}, {})
