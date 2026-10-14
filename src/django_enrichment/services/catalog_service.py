# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Field catalog passthrough (contract A3 § 2) — the adapter's optional `describe_fields` /
`channel_languages`, for the CMS run dialog. An adapter without the extra → `NotSupportedError`."""

from django.core.exceptions import ObjectDoesNotExist

from django_enrichment.adapters import get_adapter


class NotSupportedError(Exception):
    """The target module's adapter does not offer this extra (→ 404 `NOT_SUPPORTED`)."""


def describe_fields(*, target_module: str, channel: str, language: str = "") -> list[dict]:
    describe = _extra(target_module, "describe_fields")
    return _call(describe, channel=channel, language=language)


def channel_languages(*, target_module: str, channel: str) -> list[str]:
    return _call(_extra(target_module, "channel_languages"), channel=channel)


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
