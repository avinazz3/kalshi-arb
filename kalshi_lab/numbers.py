"""Exact numeric inputs shared by market metadata and fee arithmetic."""
from decimal import Decimal, InvalidOperation


def decimal(value):
    if isinstance(value, (float, bool)):
        raise ValueError("Use exact strings, integers or Decimal, not float/bool")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid decimal value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError("Non-finite value")
    return number
