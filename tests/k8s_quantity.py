"""A Kubernetes quantity read as a number: cores for CPU, bytes for memory and sizes.

The forms read are the ones the declarations write: a plain number, a
millicore suffix, and the binary suffixes. Any other suffix is not a number
here, so it raises rather than reading as one.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

__all__ = ('MIB', 'quantity')

#: One mebibyte, the unit a declaration's memory terms are written in.
MIB = Decimal(2**20)

#: The scale of each suffix read.
_SUFFIXES = {'m': Decimal('0.001'), 'Ki': Decimal(2**10), 'Mi': MIB, 'Gi': Decimal(2**30)}


def quantity(text: str) -> Decimal:
    """`text` in cores or bytes."""
    number, scale = text, Decimal(1)
    for suffix, factor in _SUFFIXES.items():
        if text.endswith(suffix):
            number, scale = text.removesuffix(suffix), factor
            break
    try:
        return Decimal(number) * scale
    except InvalidOperation:
        raise ValueError(f'{text!r} is not a quantity this reader knows') from None
