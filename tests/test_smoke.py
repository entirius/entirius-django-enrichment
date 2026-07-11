# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.


def test_app_config_loaded():
    from django.apps import apps

    config = apps.get_app_config("django_enrichment")
    assert config.name == "django_enrichment"


def test_package_importable():
    import django_enrichment

    assert django_enrichment.__name__ == "django_enrichment"
