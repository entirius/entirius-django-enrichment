# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Augmentation `cover_product` — the facts of the product the brief puts on the cover (`brief.cover_product` =
`{channel, ref}`), read through its adapter's optional `build_context`, so a scene can show what the product is.

No product in the brief, or an adapter without `build_context`: no block. An unknown product fails the subject.
"""

import json
from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from django_enrichment.adapters import get_adapter
from django_enrichment.augmentations import AugmentResult
from django_enrichment.kinds.base import SubjectError

BRIEF_FIELD = "cover_product"
PRODUCT_MODULE = "pim"


def run(*, task: Any, subject_ref: str, context: dict) -> AugmentResult:  # noqa: ARG001
    product = (context.get("brief") or {}).get(BRIEF_FIELD)
    if not isinstance(product, dict) or not product.get("ref"):
        return AugmentResult("")
    build_context = getattr(get_adapter(PRODUCT_MODULE), "build_context", None)
    if build_context is None:
        return AugmentResult("")
    ref, channel = str(product["ref"]), str(product.get("channel") or "")
    try:
        facts = build_context(subject_ref=ref, channel=channel, language=str(context.get("language") or ""))
    except ObjectDoesNotExist as exc:
        raise SubjectError("PRODUCT_NOT_FOUND", f"product {ref!r} not found in channel {channel!r}") from exc
    return AugmentResult(json.dumps(facts, ensure_ascii=False))
