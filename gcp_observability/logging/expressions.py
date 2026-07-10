"""
Low-level expression tree for Cloud Logging filter language.

Usage:
    from gcp_observability.logging.expressions import F, And, Or, Not, Raw

    expr = (
        (F("resource.type") == "cloud_run_revision")
        & (F("severity") >= "ERROR")
        & ~(F("textPayload").has("healthcheck"))
    )
    print(expr.build())
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

_SAFE_VALUE = re.compile(r"^[a-zA-Z0-9_]+$")


def _format_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if _SAFE_VALUE.match(s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class Expr(ABC):
    @abstractmethod
    def build(self) -> str: ...

    def __and__(self, other: Expr) -> And:
        if isinstance(self, And):
            return And(*self.exprs, other)
        return And(self, other)

    def __or__(self, other: Expr) -> Or:
        if isinstance(self, Or):
            return Or(*self.exprs, other)
        return Or(self, other)

    def __invert__(self) -> Not:
        return Not(self)

    def __str__(self) -> str:
        return self.build()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.build()!r})"


class Comparison(Expr):
    def __init__(self, field: str, op: str, value: object) -> None:
        self.field = field
        self.op = op
        self.value = value

    def build(self) -> str:
        return f"{self.field}{self.op}{_format_value(self.value)}"


class And(Expr):
    def __init__(self, *exprs: Expr) -> None:
        flat: list[Expr] = []
        for e in exprs:
            if isinstance(e, And):
                flat.extend(e.exprs)
            else:
                flat.append(e)
        self.exprs = tuple(flat)

    def build(self) -> str:
        parts: list[str] = []
        for e in self.exprs:
            s = e.build()
            parts.append(f"({s})" if isinstance(e, Or) else s)
        return "\nAND ".join(parts)


class Or(Expr):
    def __init__(self, *exprs: Expr) -> None:
        flat: list[Expr] = []
        for e in exprs:
            if isinstance(e, Or):
                flat.extend(e.exprs)
            else:
                flat.append(e)
        self.exprs = tuple(flat)

    def build(self) -> str:
        return " OR ".join(f"({e.build()})" for e in self.exprs)


class Not(Expr):
    def __init__(self, expr: Expr) -> None:
        self.expr = expr

    def build(self) -> str:
        inner = self.expr.build()
        if isinstance(self.expr, (And, Or)):
            return f"NOT ({inner})"
        return f"NOT {inner}"


class Raw(Expr):
    """Pass a raw filter string through unchanged."""

    def __init__(self, filter_str: str) -> None:
        self.filter_str = filter_str

    def build(self) -> str:
        return self.filter_str


class Field:
    """
    A field reference that produces Expr objects via comparison operators.

    Examples:
        F("severity") >= "ERROR"          -> Comparison("severity", ">=", "ERROR")
        F("resource.type") == "gce_instance"
        F("textPayload").has("error")     -> Comparison("textPayload", ":", "error")
        F("labels")["my-label"] == "v1"  -> labels with special-char key
        F("resource").labels.zone == "us-central1-a"   # chained via dot
    """

    __slots__ = ("_name",)
    _name: str  # slot type annotation so type checkers don't resolve via __getattr__

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    # Attribute access builds dot-notation field paths: F("resource").type
    def __getattr__(self, attr: str) -> Field:
        return Field(f"{self._name}.{attr}")

    # Bracket access quotes the key for special-char label/field names
    def __getitem__(self, key: str) -> Field:
        return Field(f'{self._name}."{key}"')

    def __eq__(self, value: object) -> Comparison:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        return Comparison(self._name, "=", value)

    def __ne__(self, value: object) -> Comparison:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        return Comparison(self._name, "!=", value)

    def __gt__(self, value: object) -> Comparison:
        return Comparison(self._name, ">", value)

    def __lt__(self, value: object) -> Comparison:
        return Comparison(self._name, "<", value)

    def __ge__(self, value: object) -> Comparison:
        return Comparison(self._name, ">=", value)

    def __le__(self, value: object) -> Comparison:
        return Comparison(self._name, "<=", value)

    def has(self, value: str) -> Comparison:
        """Substring / has-field match using the `:` operator."""
        return Comparison(self._name, ":", value)

    def __repr__(self) -> str:
        return f"Field({self._name!r})"


def F(name: str) -> Field:
    """Shorthand for Field(name)."""
    return Field(name)
