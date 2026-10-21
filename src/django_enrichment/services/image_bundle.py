# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Image bundle — the picture variants of one artifact as one staged zip (`0.png`, `1.png`, …, stored, not compressed).

A proposal stages one file; a bundle keeps the variants of the `cover` kind behind that one ref, so staging, reject,
supersede and the retention prune need nothing new. Only the bus writes bundles; `read` still caps what it takes out.
"""

import io
import zipfile

from django.conf import settings

from django_enrichment import settings as enrichment_settings

MAX_VARIANTS = 4


def pack(images: list[bytes]) -> bytes:
    """The zip of `images`, entry `<index>.png` each."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as bundle:
        for index, image in enumerate(images):
            bundle.writestr(_name(index), image)
    return buffer.getvalue()


def read(file, index: int) -> bytes:
    """Variant `index` of the bundle in `file`. Raises `LookupError` for a missing variant or a file that is no bundle."""
    try:
        with zipfile.ZipFile(file) as bundle:
            info = bundle.getinfo(_name(index))
            if info.file_size > _max_image_bytes():
                raise LookupError(f"variant {index} is larger than an image may be")
            return bundle.read(info)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise LookupError(f"no variant {index} in this bundle") from exc


def _name(index: int) -> str:
    return f"{index}.png"


def _max_image_bytes() -> int:
    return getattr(settings, "ENRICHMENT_MAX_IMAGE_BYTES", enrichment_settings.ENRICHMENT_MAX_IMAGE_BYTES)
