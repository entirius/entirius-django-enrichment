# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Root URL config for django_enrichment — mounts the Admin API v2 namespace."""

from django.urls import include, path

urlpatterns = [path("api/enrichment/v2/admin/", include("django_enrichment.api.admin.urls"))]
