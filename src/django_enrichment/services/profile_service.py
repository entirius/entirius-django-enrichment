# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Company profile (contract A4 § 2): one row per scope, versioned, validated against `PROFILE_SCHEMA`.

The only writer of `Profile` / `ProfileVersion`: every save validates the data, bumps `version` and snapshots
it; a restore saves an old snapshot as a new version. A channel's effective profile is the instance profile
with the channel overlay's non-empty sections on top: a markdown section replaces the instance's, the list
sections (`always_rules`, `rules`, `facts`) add to it. Prompt rendering lives in `profile_context`, the CMS
health check in `profile_status`.
"""

from collections.abc import Callable

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import F, Max, QuerySet

from django_enrichment.models import EnrichmentTask, Profile, ProfileVersion
from django_enrichment.schemas.profile import SCHEMA_VERSION, validation_errors
from django_enrichment.services import profile_context, profile_status

INSTANCE = ""
# List sections an overlay adds to (the overlay wins on the same id); every other section is replaced whole.
KEYED_SECTIONS = frozenset({"rules", "facts"})
LINE_SECTIONS = frozenset({"always_rules"})


class ProfileValidationError(ValueError):
    """The data does not match the profile schema; `errors` = `[{"path", "message"}]` (API: 400 per path)."""

    def __init__(self, errors: list[dict]) -> None:
        super().__init__("the profile does not match its schema")
        self.errors = errors


def get_profile(channel: str = INSTANCE) -> Profile:
    """The scope's profile; an unsaved empty one (version 0) when nothing was saved yet."""
    return Profile.objects.filter(channel=channel).first() or Profile(channel=channel, schema_version=SCHEMA_VERSION)


def save_profile(*, channel: str, data: dict, note: str = "", user: AbstractBaseUser | None = None) -> Profile:
    """Replace the scope's data with `data` as a new version. Raises `ProfileValidationError`."""
    return update_profile(channel, lambda _current: data, note=note, user=user)


def update_profile(
    channel: str, change: Callable[[dict], dict], *, note: str = "", user: AbstractBaseUser | None = None
) -> Profile:
    """`change(current data) -> new data` under the row lock, validated, saved as the next version."""
    with transaction.atomic():
        profile, _ = Profile.objects.select_for_update().get_or_create(channel=channel)
        data = change(dict(profile.data))
        if errors := validation_errors(data):
            raise ProfileValidationError(errors)
        profile.data, profile.version, profile.updated_by = data, _next_version(profile), user
        profile.schema_version = SCHEMA_VERSION
        profile.save()
        ProfileVersion.objects.create(profile=profile, version=profile.version, data=data, note=note, created_by=user)
    return profile


def _next_version(profile: Profile) -> int:
    """One past the highest snapshot: `profile.version` can lag behind them (e.g. a fixture reloaded)."""
    latest = ProfileVersion.objects.filter(profile=profile).aggregate(latest=Max("version"))["latest"] or 0
    return max(latest, profile.version) + 1


def list_versions(channel: str = INSTANCE) -> QuerySet[ProfileVersion]:
    return ProfileVersion.objects.filter(profile__channel=channel).select_related("created_by")


def restore(channel: str, number: int, *, user: AbstractBaseUser | None = None) -> Profile:
    """Save version `number`'s data as a new version. Raises `ValueError` (not found / invalid)."""
    snapshot = list_versions(channel).filter(version=number).first()
    if snapshot is None:
        raise ValueError(f"profile version {number} not found")
    return save_profile(channel=channel, data=snapshot.data, note=f"restored version {number}", user=user)


def merged(channel: str = INSTANCE) -> dict:
    """The effective profile: instance data with the channel overlay's non-empty sections on top."""
    data = dict(get_profile(INSTANCE).data)
    if channel == INSTANCE:
        return data
    for key, value in get_profile(channel).data.items():
        if value:
            data[key] = _merged_section(key, data.get(key), value)
    return data


def _merged_section(key: str, base: object, overlay: object) -> object:
    """List sections add the overlay to the instance (the overlay wins on the same id); the rest is replaced."""
    if key in LINE_SECTIONS:
        return list(dict.fromkeys([*(base or []), *overlay]))
    if key in KEYED_SECTIONS:
        return list({item["id"]: item for item in [*(base or []), *overlay]}.values())
    return overlay


def version_ids(channel: str = INSTANCE) -> list[int]:
    """Ids of the current `ProfileVersion`s behind `merged(channel)` (instance first, then the overlay)."""
    current = ProfileVersion.objects.filter(profile__channel__in={INSTANCE, channel}, version=F("profile__version"))
    return list(current.order_by("profile__channel").values_list("pk", flat=True))


def context_for(*, channel: str, task_type: str, kind: str, sections: list[str] | None = None) -> tuple[str, str]:
    """`(instructions, data)` of the channel's profile for a prompt; both blank without a profile."""
    return profile_context.render(merged(channel), task_type=task_type, kind=kind, sections=sections)


def record_use(*, task: EnrichmentTask, channel: str) -> None:
    """Stamp the profile versions a task's prompt used (`EnrichmentTask.profile_versions`)."""
    task.profile_versions = version_ids(channel)
    EnrichmentTask.objects.filter(pk=task.pk).update(profile_versions=task.profile_versions)


def qa_check_default() -> bool:
    """Whether new AI text proposals are checked against the checklists when a run does not say (instance row)."""
    return bool(Profile.objects.filter(channel=INSTANCE).values_list("qa_check", flat=True).first())


def set_qa_check_default(enabled: bool) -> bool:
    """Store the instance default of the automatic checklist check (no profile version: a setting, not content)."""
    profile, _ = Profile.objects.get_or_create(channel=INSTANCE)
    profile.qa_check = enabled
    profile.save(update_fields=["qa_check", "modified_at"])
    return profile.qa_check


def status(channel: str = INSTANCE) -> dict:
    """Completeness of the channel's effective profile, per section (CMS health)."""
    sections = profile_status.sections(merged(channel))
    return {"channel": channel, "complete": all(item["complete"] for item in sections), "sections": sections}
