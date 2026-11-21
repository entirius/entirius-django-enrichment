# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Enrichment bus of the Entirius platform: tasks, workers, proposals and review."""

from importlib.metadata import PackageNotFoundError, version

# munin reports it to the CMS, which shows the screens of newer releases only when it is high enough.
try:
    __version__ = version("entirius-django-enrichment")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = ""
