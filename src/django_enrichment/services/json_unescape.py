# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Strings of a model's JSON answer with the escapes it wrote twice restored (a leaf: no Django, no services).

Only `\\"` and `\\n` are restored, and only where the backslash is not itself escaped (`\\\\"` stays): content
never has a backslash before a quote or an `n`, other sequences (`\\t`, `C:\\dir`) are left alone.
"""

import re
from typing import Any

_ESCAPE = re.compile(r'\\([\\"n])')
_RESTORED = {'"': '"', "n": "\n"}


def unescape_strings(value: Any) -> Any:
    """Strings of `value` (any depth, keys untouched) with the `\\"` and `\\n` of a twice-escaped JSON restored."""
    if isinstance(value, str):
        return _ESCAPE.sub(_restore, value)
    if isinstance(value, list):
        return [unescape_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: unescape_strings(item) for key, item in value.items()}
    return value


def _restore(match: re.Match[str]) -> str:
    """An escaped backslash is consumed as a pair, so the character after it is never taken for an escape."""
    return _RESTORED.get(match.group(1), match.group())
