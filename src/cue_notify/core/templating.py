"""Message templating with sandboxed Jinja2.

Templates are authored through the API, so they run in Jinja's immutable sandbox: no
access to Python internals, no mutation of data. On top of that, operations that can
exhaust memory or CPU are bounded — large powers, sequence repetition, ``%``/``format``
padding, oversized ``range`` loops and oversized output all raise :class:`RenderError`.

Undefined variables raise instead of silently rendering as an empty string — a
notification that reads "Your order  has shipped" is worse than one that is never sent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, cast

from jinja2 import StrictUndefined, TemplateError, Undefined
from jinja2.exceptions import SecurityError
from jinja2.runtime import Context
from jinja2.sandbox import ImmutableSandboxedEnvironment

MAX_OUTPUT = 20_000
"""Longest rendered field, in characters."""
MAX_RANGE = 1_000
MAX_SEQUENCE = 10_000
MAX_EXPONENT = 64
MAX_PADDING = 200
_PADDING_METHODS = frozenset(
    {"ljust", "rjust", "center", "zfill", "expandtabs", "format", "format_map"}
)


class RenderError(ValueError):
    """A template failed to compile or render."""


def _format_number(value: Any, decimals: int = 0, separator: str = " ", point: str = ".") -> str:
    """``{{ amount | number }}`` → ``1 250 000``; ``{{ rate | number(2, ",", ".") }}``."""
    if isinstance(value, Undefined):
        raise RenderError("number filter received an undefined value")
    try:
        quantized = Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals))
    except (InvalidOperation, ValueError) as exc:
        raise RenderError(f"number filter cannot format {value!r}") from exc
    sign = "-" if quantized < 0 else ""
    integer, _, fraction = f"{abs(quantized):f}".partition(".")
    groups: list[str] = []
    while integer:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    text = separator.join(groups) or "0"
    return f"{sign}{text}{point}{fraction}" if fraction else f"{sign}{text}"


class _BoundedSandbox(ImmutableSandboxedEnvironment):
    """The immutable sandbox plus resource limits on otherwise-unbounded operations."""

    # Intercepting these also stops Jinja from constant-folding them at compile time.
    intercepted_binops = frozenset({"*", "**", "%"})

    def call_binop(self, context: Context, operator: str, left: Any, right: Any) -> Any:
        if operator == "**" and (not isinstance(right, int | float) or abs(right) > MAX_EXPONENT):
            raise SecurityError(f"exponent larger than {MAX_EXPONENT} is not allowed")
        if operator == "*":
            for sequence, times in ((left, right), (right, left)):
                if (
                    isinstance(sequence, str | list | tuple)
                    and isinstance(times, int)
                    and len(cast("Any", sequence)) * times > MAX_SEQUENCE
                ):
                    raise SecurityError("sequence repetition result is too large")
        if operator == "%" and isinstance(left, str):
            raise SecurityError("'%' string formatting is not allowed; use filters")
        return super().call_binop(context, operator, left, right)

    def wrap_str_format(self, value: Any) -> Any:
        # Jinja wraps str.format before the attribute safety check; refuse it outright.
        if super().wrap_str_format(value) is None:
            return None

        def blocked(*_: Any, **__: Any) -> str:
            raise SecurityError("str.format is not allowed; use string concatenation (~)")

        return blocked

    def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
        if isinstance(obj, str) and attr in _PADDING_METHODS:
            return False  # unbounded padding/format widths
        return super().is_safe_attribute(obj, attr, value)


def _bounded_range(*args: int) -> range:
    result = range(*args)
    if len(result) > MAX_RANGE:
        raise SecurityError(f"range() is limited to {MAX_RANGE} items")
    return result


def _no_format(*_: Any, **__: Any) -> str:
    raise SecurityError("the format filter is not allowed; use string concatenation (~)")


def _capped(filter_name: str, limit: int) -> Any:
    def wrapper(value: Any, width: Any = 80, *args: Any, **kwargs: Any) -> Any:
        if isinstance(width, int) and width > limit:
            raise SecurityError(f"{filter_name} width is limited to {limit}")
        return original(value, width, *args, **kwargs)

    original = cast("Callable[..., Any]", ImmutableSandboxedEnvironment().filters[filter_name])
    return wrapper


def _environment(*, autoescape: bool) -> ImmutableSandboxedEnvironment:
    env = _BoundedSandbox(
        undefined=StrictUndefined,
        autoescape=autoescape,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )
    env.filters["number"] = _format_number
    env.filters["format"] = _no_format
    cast("dict[str, Any]", env.globals)["range"] = _bounded_range
    for name in ("center", "indent"):
        env.filters[name] = _capped(name, MAX_PADDING)
    return env


_TEXT_ENV = _environment(autoescape=False)
_HTML_ENV = _environment(autoescape=True)


@lru_cache(maxsize=1024)
def _compile(source: str, html: bool) -> Any:
    env = _HTML_ENV if html else _TEXT_ENV
    try:
        return env.from_string(source)
    except TemplateError as exc:
        raise RenderError(f"invalid template: {exc}") from exc


def check(source: str, *, html: bool = False) -> None:
    """Validate template syntax without rendering. Raises :class:`RenderError`."""
    _compile(source, html)


def render(source: str, context: Mapping[str, Any], *, html: bool = False) -> str:
    template = _compile(source, html)
    try:
        chunks: list[str] = []
        size = 0
        for chunk in template.generate(**context):
            size += len(chunk)
            if size > MAX_OUTPUT:
                raise RenderError(f"rendered text exceeds {MAX_OUTPUT} characters")
            chunks.append(chunk)
        result = "".join(chunks)
    except RenderError:
        raise
    except TemplateError as exc:
        raise RenderError(str(exc)) from exc
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise RenderError(f"{type(exc).__name__}: {exc}") from exc
    return result if html else result.strip()
