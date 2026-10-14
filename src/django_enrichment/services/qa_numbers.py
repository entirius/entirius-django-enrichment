# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Numbers in text, compared by value (a leaf: no Django, no services).

A number is an integer or a decimal with a comma or a dot, with optional thousands groups (space, no-break
space, comma or dot); a percentage or a range is checked number by number. Every token has its candidate
values (`"1,299"` may be 1299 or 1.299), and two tokens match when their candidates overlap — so `12,5 %`
matches `12.5%` and `1 299,99` matches `1299.99`. Digits glued to a preceding letter (`A4`, `B2B`) are names,
not numbers.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_SPACES = "   "
NUMBER = re.compile(rf"(?<![^\W\d_])(?<![\d.,])\d+(?:(?:[.,]|[{_SPACES}](?=\d{{3}}(?!\d)))\d+)*")
_MARKUP = re.compile(r"<[^>]*>|&#?\w+;")


@dataclass(frozen=True)
class NumberToken:
    text: str
    start: int
    end: int
    values: frozenset[str]


def tokens(text: str) -> Iterator[NumberToken]:
    """Every number in `text` (HTML tags and entities skipped; offsets refer to `text` itself)."""
    plain = _MARKUP.sub(lambda match: " " * len(match.group()), text)
    for match in NUMBER.finditer(plain):
        yield NumberToken(match.group(), match.start(), match.end(), candidates(match.group()))


def values_in(text: str) -> set[str]:
    """All candidate values of all numbers in `text`."""
    return {value for token in tokens(text) for value in token.values}


def candidates(token: str) -> frozenset[str]:
    """Canonical values a number token may mean; a space-grouped token also means each of its parts."""
    found = set(_values(re.sub(f"[{_SPACES}]", "", token)))
    if any(space in token for space in _SPACES):
        found |= {value for part in re.split(f"[{_SPACES}]", token) for value in _values(part)}
    return frozenset(found)


def _values(token: str) -> set[str]:
    groups = re.split(r"[.,]", token)
    if len(groups) == 1:
        return {_canonical(token)}
    found = set()
    if all(len(group) == 3 for group in groups[1:-1]):
        found.add(_canonical(f"{''.join(groups[:-1])}.{groups[-1]}"))  # the last separator is the decimal point
    if all(len(group) == 3 for group in groups[1:]):
        found.add(_canonical("".join(groups)))  # every separator groups thousands
    return found or {token}


def _canonical(number: str) -> str:
    try:
        return format(Decimal(number).normalize(), "f")
    except InvalidOperation:
        return number
