"""Explicit units and bounded decimal inputs shared by the model and CLI."""

from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import re


def arithmetic_context():
    # A fresh context also isolates rounding, traps and exponent bounds from
    # caller/thread/task configuration. Setting precision alone is insufficient.
    return localcontext(
        Context(prec=80, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)
    )


UNITS = {
    "B": 1,
    **{
        name + "B": 1000**index
        for index, name in enumerate(("K", "M", "G", "T", "P"), 1)
    },
    **{
        name + "iB": 1024**index
        for index, name in enumerate(("K", "M", "G", "T", "P"), 1)
    },
}


def number(value, low=0, high="1e30", low_open=False):
    if (
        isinstance(value, bool)
        or isinstance(value, float)
        or not isinstance(value, (str, int, Decimal))
    ):
        raise ValueError("use a decimal string or integer, never a binary float/bool")
    if isinstance(value, str) and (
        len(value) > 100
        or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value)
    ):
        raise ValueError("invalid bounded decimal input")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if (
        not result.is_finite()
        or result < Decimal(str(low))
        or result > Decimal(str(high))
        or (low_open and result == Decimal(str(low)))
    ):
        raise ValueError("decimal outside allowed range")
    if not -30 <= result.as_tuple().exponent <= 30:
        raise ValueError("decimal exponent exceeds reference precision limits")
    return result


def text(value):
    return (
        format(value, "f").rstrip("0").rstrip(".")
        if "." in format(value, "f")
        else format(value, "f")
    )


def parse_bytes(value):
    if not isinstance(value, str) or len(value) > 120:
        raise ValueError("byte quantity must be a string with explicit units")
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s+(B|KB|MB|GB|TB|PB|KiB|MiB|GiB|TiB|PiB)\s*", value
    )
    if not match:
        raise ValueError(
            "use e.g. '1 MB' (decimal) or '1 MiB' (binary); lowercase b is bits"
        )
    with arithmetic_context():
        result = number(match[1]) * UNITS[match[2]]
        if result != result.to_integral_value():
            raise ValueError("a byte quantity must resolve to whole bytes")
        return result
