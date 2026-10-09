"""A safe, dependency-free JSON Logic evaluator (https://jsonlogic.com).

Rules are data, not code: they are stored in the database, edited through the API and
evaluated against untrusted event payloads, so the evaluator never executes arbitrary
Python. It implements the standard operator set with JSON Logic's (JavaScript-flavoured)
truthiness and loose-equality semantics, plus a few string helpers that notification
rules need constantly.

>>> evaluate({">": [{"var": "event.data.amount"}, 100]}, {"event": {"data": {"amount": 250}}})
True
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

type Rule = Any
type Data = Any

MAX_DEPTH = 64


class LogicError(ValueError):
    """Raised for malformed rules (unknown operators, wrong arity, excessive nesting)."""


# --------------------------------------------------------------------------- semantics


def truthy(value: Any) -> bool:
    """JSON Logic truthiness: ``[]`` is falsy, ``"0"`` is truthy, ``NaN`` is falsy."""
    if isinstance(value, list):
        return len(cast("list[Any]", value)) > 0
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(value)


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return 0.0
        try:
            return float(stripped)
        except ValueError:
            return None
    if value is None:
        return 0.0
    return None


def _loose_equals(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if type(a) is type(b) or (isinstance(a, str) and isinstance(b, str)):
        return bool(a == b)
    na, nb = _to_number(a), _to_number(b)
    if na is not None and nb is not None:
        return na == nb
    return str(a) == str(b)


def _strict_equals(a: Any, b: Any) -> bool:
    numeric = (int, float)
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, numeric) and isinstance(b, numeric):
        return a == b
    return type(a) is type(b) and bool(a == b)


def _compare(a: Any, b: Any) -> tuple[Any, Any] | None:
    if isinstance(a, str) and isinstance(b, str):
        return a, b
    na, nb = _to_number(a), _to_number(b)
    if na is None or nb is None:
        return None
    return na, nb


def _less(a: Any, b: Any, *, or_equal: bool) -> bool:
    pair = _compare(a, b)
    if pair is None:
        return False
    x, y = pair
    return bool(x <= y) if or_equal else bool(x < y)


def _number(value: Any) -> float:
    result = _to_number(value)
    return math.nan if result is None else result


def _normalise_number(value: float) -> int | float:
    return int(value) if value.is_integer() and abs(value) < 2**53 else value


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(_normalise_number(value))
    return str(value)


def resolve_var(data: Data, path: Any, default: Any = None) -> Any:
    """Resolve a dotted path (``"user.tags.0"``) against nested mappings and sequences."""
    if path is None or path == "":
        return data
    current = data
    for part in str(path).split("."):
        if isinstance(current, Mapping):
            mapping = cast("Mapping[str, Any]", current)
            if part not in mapping:
                return default
            current = mapping[part]
        elif isinstance(current, Sequence) and not isinstance(current, str):
            try:
                current = cast("Sequence[Any]", current)[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return current


# --------------------------------------------------------------------------- operators

type Evaluator = Callable[[Rule, Data, int], Any]
type Operator = Callable[[list[Any], Data, Evaluator, int], Any]


def _eager(fn: Callable[..., Any]) -> Operator:
    """Wrap an operator whose arguments are all evaluated up front."""

    def op(args: list[Any], data: Data, ev: Evaluator, depth: int) -> Any:
        return fn(*(ev(arg, data, depth) for arg in args))

    return op


def _op_var(args: list[Any], data: Data, ev: Evaluator, depth: int) -> Any:
    values = [ev(arg, data, depth) for arg in args]
    path = values[0] if values else None
    default = values[1] if len(values) > 1 else None
    if isinstance(path, int | float) and not isinstance(path, bool):
        path = str(_normalise_number(float(path)))
    return resolve_var(data, path, default)


def _op_missing(args: list[Any], data: Data, ev: Evaluator, depth: int) -> list[Any]:
    keys = [ev(arg, data, depth) for arg in args]
    if len(keys) == 1 and isinstance(keys[0], list):
        keys = cast("list[Any]", keys[0])
    sentinel = object()
    return [key for key in keys if resolve_var(data, key, sentinel) in (sentinel, None, "")]


def _op_missing_some(args: list[Any], data: Data, ev: Evaluator, depth: int) -> list[Any]:
    need = int(_number(ev(args[0], data, depth)))
    keys = cast("list[Any]", ev(args[1], data, depth))
    missing = _op_missing([keys], data, ev, depth)
    return [] if len(keys) - len(missing) >= need else missing


def _op_if(args: list[Any], data: Data, ev: Evaluator, depth: int) -> Any:
    index = 0
    while index < len(args) - 1:
        if truthy(ev(args[index], data, depth)):
            return ev(args[index + 1], data, depth)
        index += 2
    return ev(args[index], data, depth) if index < len(args) else None


def _op_and(args: list[Any], data: Data, ev: Evaluator, depth: int) -> Any:
    result: Any = None
    for arg in args:
        result = ev(arg, data, depth)
        if not truthy(result):
            return result
    return result


def _op_or(args: list[Any], data: Data, ev: Evaluator, depth: int) -> Any:
    result: Any = None
    for arg in args:
        result = ev(arg, data, depth)
        if truthy(result):
            return result
    return result


def _op_less(or_equal: bool) -> Operator:
    def op(args: list[Any], data: Data, ev: Evaluator, depth: int) -> bool:
        values = [ev(arg, data, depth) for arg in args]
        if len(values) == 3:  # "between": a < b < c
            return _less(values[0], values[1], or_equal=or_equal) and _less(
                values[1], values[2], or_equal=or_equal
            )
        return _less(values[0], values[1], or_equal=or_equal)

    return op


def _arithmetic_add(*values: Any) -> float | int:
    return _normalise_number(math.fsum(_number(v) for v in values))


def _arithmetic_mul(*values: Any) -> float | int:
    return _normalise_number(math.prod(_number(v) for v in values))


def _arithmetic_sub(a: Any, b: Any = None) -> float | int:
    if b is None:
        return _normalise_number(-_number(a))
    return _normalise_number(_number(a) - _number(b))


def _arithmetic_div(a: Any, b: Any) -> float | int | None:
    divisor = _number(b)
    if divisor == 0:
        return None
    return _normalise_number(_number(a) / divisor)


def _arithmetic_mod(a: Any, b: Any) -> float | int | None:
    divisor = _number(b)
    if divisor == 0:
        return None
    return _normalise_number(math.fmod(_number(a), divisor))


def _minmax(pick: Callable[..., float]) -> Callable[..., float | int | None]:
    def op(*values: Any) -> float | int | None:
        numbers = [_number(v) for v in values]
        if not numbers or any(math.isnan(n) for n in numbers):
            return None
        return _normalise_number(pick(numbers))

    return op


def _op_in(needle: Any, haystack: Any) -> bool:
    if isinstance(haystack, str):
        return _as_text(needle) in haystack
    if isinstance(haystack, list):
        return any(_strict_equals(needle, item) for item in cast("list[Any]", haystack))
    return False


def _op_substr(source: Any, start: Any, length: Any = None) -> str:
    text = _as_text(source)
    begin = int(_number(start))
    if begin < 0:
        begin = max(len(text) + begin, 0)
    if length is None:
        return text[begin:]
    count = int(_number(length))
    end = len(text) + count if count < 0 else begin + count
    return text[begin:end]


def _op_merge(*values: Any) -> list[Any]:
    merged: list[Any] = []
    for value in values:
        if isinstance(value, list):
            merged.extend(cast("list[Any]", value))
        else:
            merged.append(value)
    return merged


def _iteration(kind: str) -> Operator:
    def op(args: list[Any], data: Data, ev: Evaluator, depth: int) -> Any:
        items = ev(args[0], data, depth) if args else []
        if not isinstance(items, list):
            items = []
        items = cast("list[Any]", items)
        body = args[1] if len(args) > 1 else None
        if kind == "map":
            return [ev(body, item, depth) for item in items]
        if kind == "filter":
            return [item for item in items if truthy(ev(body, item, depth))]
        if kind == "all":
            return bool(items) and all(truthy(ev(body, item, depth)) for item in items)
        if kind == "some":
            return any(truthy(ev(body, item, depth)) for item in items)
        if kind == "none":
            return not any(truthy(ev(body, item, depth)) for item in items)
        raise AssertionError(kind)

    return op


def _op_reduce(args: list[Any], data: Data, ev: Evaluator, depth: int) -> Any:
    items = ev(args[0], data, depth)
    accumulator = ev(args[2], data, depth) if len(args) > 2 else None
    if not isinstance(items, list):
        return accumulator
    for item in cast("list[Any]", items):
        accumulator = ev(args[1], {"current": item, "accumulator": accumulator}, depth)
    return accumulator


def _not(value: Any = None) -> bool:
    return not truthy(value)


def _truthy(value: Any = None) -> bool:
    return truthy(value)


def _loose_not_equals(a: Any, b: Any) -> bool:
    return not _loose_equals(a, b)


def _strict_not_equals(a: Any, b: Any) -> bool:
    return not _strict_equals(a, b)


def _greater(a: Any, b: Any) -> bool:
    return _less(b, a, or_equal=False)


def _greater_or_equal(a: Any, b: Any) -> bool:
    return _less(b, a, or_equal=True)


def _cat(*values: Any) -> str:
    return "".join(_as_text(value) for value in values)


def _starts_with(text: Any, prefix: Any) -> bool:
    return _as_text(text).startswith(_as_text(prefix))


def _ends_with(text: Any, suffix: Any) -> bool:
    return _as_text(text).endswith(_as_text(suffix))


def _lower(text: Any) -> str:
    return _as_text(text).lower()


def _upper(text: Any) -> str:
    return _as_text(text).upper()


OPERATORS: dict[str, Operator] = {
    # data access
    "var": _op_var,
    "missing": _op_missing,
    "missing_some": _op_missing_some,
    # logic
    "if": _op_if,
    "?:": _op_if,
    "and": _op_and,
    "or": _op_or,
    "!": _eager(_not),
    "!!": _eager(_truthy),
    "==": _eager(_loose_equals),
    "!=": _eager(_loose_not_equals),
    "===": _eager(_strict_equals),
    "!==": _eager(_strict_not_equals),
    # comparison
    "<": _op_less(or_equal=False),
    "<=": _op_less(or_equal=True),
    ">": _eager(_greater),
    ">=": _eager(_greater_or_equal),
    "max": _eager(_minmax(max)),
    "min": _eager(_minmax(min)),
    # arithmetic
    "+": _eager(_arithmetic_add),
    "-": _eager(_arithmetic_sub),
    "*": _eager(_arithmetic_mul),
    "/": _eager(_arithmetic_div),
    "%": _eager(_arithmetic_mod),
    # arrays
    "map": _iteration("map"),
    "filter": _iteration("filter"),
    "all": _iteration("all"),
    "some": _iteration("some"),
    "none": _iteration("none"),
    "reduce": _op_reduce,
    "merge": _eager(_op_merge),
    "in": _eager(_op_in),
    # strings
    "cat": _eager(_cat),
    "substr": _eager(_op_substr),
    # Cue extensions — common in notification targeting.
    "starts_with": _eager(_starts_with),
    "ends_with": _eager(_ends_with),
    "lower": _eager(_lower),
    "upper": _eager(_upper),
}


# --------------------------------------------------------------------------- public API


def _is_operation(rule: Any) -> bool:
    return isinstance(rule, Mapping) and len(cast("Mapping[str, Any]", rule)) == 1


def _evaluate(rule: Rule, data: Data, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise LogicError(f"rule nesting exceeds {MAX_DEPTH} levels")
    if isinstance(rule, list):
        return [_evaluate(item, data, depth + 1) for item in cast("list[Any]", rule)]
    if not _is_operation(rule):
        return rule
    ((name, raw_args),) = cast("Mapping[str, Any]", rule).items()
    operator = OPERATORS.get(name)
    if operator is None:
        raise LogicError(f"unknown operator {name!r}")
    args = cast("list[Any]", raw_args) if isinstance(raw_args, list) else [raw_args]
    try:
        return operator(args, data, _evaluate, depth + 1)
    except LogicError:
        raise
    except (TypeError, IndexError) as exc:
        raise LogicError(f"invalid arguments for {name!r}: {exc}") from exc


def evaluate(rule: Rule, data: Data) -> Any:
    """Evaluate ``rule`` against ``data`` and return the raw result."""
    return _evaluate(rule, data, 0)


def matches(rule: Rule | None, data: Data) -> bool:
    """Evaluate a condition. ``None`` (no condition) always matches."""
    if rule is None:
        return True
    return truthy(evaluate(rule, data))


def validate(rule: Rule, *, _depth: int = 0) -> None:
    """Statically check that every operator in ``rule`` is known. Raises :class:`LogicError`."""
    if _depth > MAX_DEPTH:
        raise LogicError(f"rule nesting exceeds {MAX_DEPTH} levels")
    if isinstance(rule, list):
        for item in cast("list[Any]", rule):
            validate(item, _depth=_depth + 1)
        return
    if not _is_operation(rule):
        return
    ((name, args),) = cast("Mapping[str, Any]", rule).items()
    if name not in OPERATORS:
        raise LogicError(f"unknown operator {name!r}")
    validate(args, _depth=_depth + 1)
