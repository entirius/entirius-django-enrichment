# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""`unescape_strings`: double-escaped quotes and newlines of a model's JSON answer, nothing else."""

import pytest

from django_enrichment.services.json_unescape import unescape_strings

BS = "\\"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (r"SERP dla \"wyprawka dla noworodka\" (18 100)", 'SERP dla "wyprawka dla noworodka" (18 100)'),
        (r"first\nsecond\n\nthird", "first\nsecond\n\nthird"),
        (r"\"quoted\"\nnext", '"quoted"\nnext'),
        (
            f'an escaped backslash {BS}{BS}" before a quote stays',
            f'an escaped backslash {BS}{BS}" before a quote stays',
        ),
        (r"a \\n stays", r"a \\n stays"),
        (f'{BS}{BS}{BS}"b', f'{BS}{BS}"b'),
        (r"tab \t, path C:\dir, slash \/, unicode \u00e9", r"tab \t, path C:\dir, slash \/, unicode \u00e9"),
        ('plain, with a real "quote" and\na real newline', 'plain, with a real "quote" and\na real newline'),
        ("", ""),
    ],
)
def test_a_string_gets_only_its_double_escaped_quotes_and_newlines_back(raw, clean):
    assert unescape_strings(raw) == clean


def test_every_string_of_a_nested_value_is_cleaned():
    raw = {"notes": r"a \"b\"", "faq": [{"q": r"\"Q\"?", "a": [r"one\ntwo", 3]}], "deep": {"deeper": [[r"\"x\""]]}}
    assert unescape_strings(raw) == {
        "notes": 'a "b"',
        "faq": [{"q": '"Q"?', "a": ["one\ntwo", 3]}],
        "deep": {"deeper": [['"x"']]},
    }


def test_other_types_are_untouched():
    value = {"count": 3, "ratio": 0.5, "on": True, "none": None, "items": []}
    assert unescape_strings(value) == value
    assert [unescape_strings(item) for item in (7, 1.5, False, None)] == [7, 1.5, False, None]


def test_keys_are_untouched():
    assert unescape_strings({r"a\"b": r"c\"d", r"line\nkey": 1}) == {r"a\"b": 'c"d', r"line\nkey": 1}


def test_the_input_is_not_modified():
    raw = {"notes": [r"a \"b\""]}
    unescape_strings(raw)
    assert raw == {"notes": [r"a \"b\""]}
