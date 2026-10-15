# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Field catalog passthrough (contract A3 § 2) — the adapter's optional `describe_fields` /
`channel_languages` / `subject_labels` / `list_pictures`, for the CMS run dialog. An adapter without the extra → `NotSupportedError`."""

from django.core.exceptions import ObjectDoesNotExist

from django_enrichment.adapters import get_adapter


class NotSupportedError(Exception):
    """The target module's adapter does not offer this extra (→ 404 `NOT_SUPPORTED`)."""


def describe_fields(*, target_module: str, channel: str, language: str = "") -> list[dict]:
    describe = _extra(target_module, "describe_fields")
    return _call(describe, channel=channel, language=language)


def channel_languages(*, target_module: str, channel: str) -> list[str]:
    return _call(_extra(target_module, "channel_languages"), channel=channel)


def resolve_subjects(*, target_module: str, channel: str, refs: list[str]) -> dict:
    """`{known: [{ref, label}], unknown: [ref]}` — the refs (trimmed, blanks dropped, deduped, order kept) split by
    whether the subject exists in the channel (adapter `subject_labels`: `{ref: label}` of the existing ones)."""
    refs = list(dict.fromkeys(ref.strip() for ref in refs if ref.strip()))
    subject_labels = _extra(target_module, "subject_labels")
    labels = _call(subject_labels, channel=channel, refs=refs) if refs else {}
    return {
        "known": [{"ref": ref, "label": labels[ref]} for ref in refs if ref in labels],
        "unknown": [ref for ref in refs if ref not in labels],
    }


def subject_pictures(*, target_module: str, channel: str, ref: str) -> list[dict]:
    """`[{ref, label, url, is_main}]` — the pictures of one subject (adapter `list_pictures`), main first."""
    list_pictures = _extra(target_module, "list_pictures")
    try:
        return list_pictures(subject_ref=ref, channel=channel)
    except ObjectDoesNotExist as exc:
        raise ValueError(f"item {ref!r} in channel {channel!r} not found") from exc


def _extra(target_module: str, name: str):
    if (extra := getattr(get_adapter(target_module), name, None)) is None:
        raise NotSupportedError(f"module {target_module!r} does not support {name}")
    return extra


def _call(extra, **kwargs):
    """An unknown channel inside the adapter is a lookup miss, not a server error."""
    try:
        return extra(**kwargs)
    except ObjectDoesNotExist as exc:
        raise ValueError(f"channel {kwargs['channel']!r} not found") from exc
