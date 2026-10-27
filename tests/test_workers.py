# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Worker model, kind registry and worker_service (validation, versioning, params, admin save)."""

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError

from django_enrichment.admin.worker import WorkerAdmin
from django_enrichment.kinds import get_kind
from django_enrichment.kinds.field import FieldKind
from django_enrichment.kinds.translate import TranslateKind
from django_enrichment.models import Worker
from django_enrichment.services import worker_service


def test_base_kinds_are_registered_on_ready():
    assert get_kind("translate") is TranslateKind
    assert get_kind("field") is FieldKind


def test_unknown_kind_raises_value_error():
    with pytest.raises(ValueError, match="unknown worker kind"):
        get_kind("nope")


@pytest.mark.django_db
class TestClean:
    def test_valid_worker_saves_at_version_1(self, make_worker):
        worker = make_worker()
        assert worker.pk is not None
        assert worker.version == 1

    def test_options_are_validated_by_the_kind(self, make_worker):
        with pytest.raises(ValidationError) as exc:
            make_worker(options={"fields": []})
        assert "options" in exc.value.message_dict

    def test_unknown_kind_is_a_validation_error(self, make_worker):
        with pytest.raises(ValidationError) as exc:
            make_worker(kind="nope")
        assert "kind" in exc.value.message_dict

    def test_admin_save_model_runs_full_clean(self, admin_user):
        admin = WorkerAdmin(Worker, AdminSite())
        worker = Worker(key="pim.bad", label="Bad", kind="field", target_module="pim", options={})
        with pytest.raises(ValidationError):
            admin.save_model(request=None, obj=worker, form=None, change=False)
        assert not Worker.objects.filter(key="pim.bad").exists()


@pytest.mark.django_db
class TestVersion:
    @pytest.mark.parametrize(
        ("field", "value"),
        [("system_prompt", "Be brief."), ("model", "claude-haiku"), ("options", {"fields": ["name"]})],
    )
    def test_calibration_change_bumps_version(self, make_worker, field, value):
        worker = make_worker()
        setattr(worker, field, value)
        assert worker_service.save_worker(worker).version == 2

    def test_other_changes_keep_version(self, make_worker):
        worker = make_worker()
        worker.label = "Renamed"
        worker.active = False
        assert worker_service.save_worker(worker).version == 1


@pytest.mark.django_db
class TestParams:
    def test_no_worker_leaves_params_untouched(self):
        assert worker_service.validate_params("fix-attribute", {"anything": 1}) == {"anything": 1}

    def test_worker_params_are_normalised(self, make_worker):
        make_worker()
        params = {"channel": "default", "source_language": "pl", "target_languages": ["en"]}
        assert worker_service.validate_params("pim.translate", params)["prompt"] == ""

    def test_invalid_params_raise_value_error(self, make_worker):
        make_worker()
        with pytest.raises(ValueError, match="target_languages"):
            worker_service.validate_params("pim.translate", {"channel": "default", "source_language": "pl"})

    def test_inactive_worker_does_not_validate(self, make_worker):
        make_worker(active=False)
        assert worker_service.validate_params("pim.translate", {}) == {}


@pytest.mark.django_db
def test_describe_carries_params_schema(make_worker):
    described = worker_service.describe(make_worker())
    assert described["key"] == "pim.translate"
    assert "target_languages" in described["params_schema"]["properties"]
