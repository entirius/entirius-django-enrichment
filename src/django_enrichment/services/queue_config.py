# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Work-queue knobs — one place that reads them at call time (host setting wins, module default otherwise)."""

from typing import Any

from django.conf import settings

from django_enrichment import settings as enrichment_settings


def get(name: str) -> Any:
    return getattr(settings, name, getattr(enrichment_settings, name))
