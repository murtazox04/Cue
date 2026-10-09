"""Message templating with sandboxed Jinja2.

Templates are authored through the API, so they run in Jinja's sandbox: no attribute
access to private members, no unsafe callables. Undefined variables raise instead of
silently rendering as an empty string — a notification that reads "Your order  has
shipped" is worse than one that is never sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from jinja2 import StrictUndefined, TemplateError, Undefined
from jinja2.sandbox import ImmutableSandboxedEnvironment


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


def _environment(*, autoescape: bool) -> ImmutableSandboxedEnvironment:
    env = ImmutableSandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=autoescape,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )
    env.filters["number"] = _format_number
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
        result = template.render(**context)
    except RenderError:
        raise
    except TemplateError as exc:
        raise RenderError(str(exc)) from exc
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise RenderError(f"{type(exc).__name__}: {exc}") from exc
    return result if html else result.strip()
