# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Company profile (contract A4 § 2): schema validation, versions, overlay, prompt context, status."""

import pytest

from django_enrichment.models import EnrichmentTask, ProfileVersion
from django_enrichment.schemas.profile import validation_errors
from django_enrichment.services import profile_context, profile_service
from django_enrichment.services.profile_service import ProfileValidationError

FACTS = [
    {"id": "f1", "label": "Founded", "value": "1999", "source": "registry", "verified_at": "2026-01-02"},
    {"id": "f2", "label": "Warranty", "value": "5 years", "verified_at": "2026-01-02"},
    {"id": "f3", "label": "Workshop", "value": "Lodz | north", "verified_at": "2026-01-02"},
]
PROFILE = {
    "brand": {"text": "We build modular sofas.", "differentiators": ["Made to order"], "never_say": ["cheap"]},
    "persona": {"personas": [{"name": "Renovator", "text": "Fixes up a flat."}]},
    "facts": FACTS,
    "tone": {"text": "Warm.", "ai_patterns": ["delve"]},
    "language": {"spellings": [{"wrong": "e-commerce", "right": "ecommerce"}]},
    "seo": {"text": "Intent first."},
    "always_rules": ["Never promise delivery dates."],
}

pytestmark = pytest.mark.django_db


def _save(data: dict, channel: str = "", note: str = "") -> object:
    return profile_service.save_profile(channel=channel, data=data, note=note)


class TestValidation:
    def test_valid_profile_has_no_errors(self):
        assert validation_errors(PROFILE) == []

    def test_errors_carry_paths(self):
        data = {"brand": {"differentiators": [1]}, "facts": [{"id": "x", "label": "L"}], "colour": "red"}
        paths = {error["path"] for error in validation_errors(data)}
        assert paths == {"data", "brand.differentiators.0", "facts.0"}

    def test_rule_pattern_must_compile(self):
        data = {"rules": [{"id": "r1", "pattern": "([", "message": "Broken"}]}
        assert validation_errors(data)[0]["path"] == "rules.0.pattern"

    def test_invalid_save_raises_and_keeps_the_old_version(self):
        _save(PROFILE)
        with pytest.raises(ProfileValidationError) as exc:
            _save({"tone": {"ai_patterns": "delve"}})
        assert exc.value.errors[0]["path"] == "tone.ai_patterns"
        assert profile_service.get_profile().version == 1


class TestVersions:
    def test_every_save_is_a_version(self, admin_user):
        _save({"always_rules": ["A"]}, note="first")
        profile = profile_service.save_profile(channel="", data={"always_rules": ["B"]}, note="second", user=admin_user)
        assert profile.version == 2
        versions = list(profile_service.list_versions())
        assert [(v.version, v.note, v.data) for v in versions] == [
            (2, "second", {"always_rules": ["B"]}),
            (1, "first", {"always_rules": ["A"]}),
        ]
        assert versions[0].created_by == admin_user

    def test_restore_writes_a_new_version(self):
        _save({"always_rules": ["A"]})
        _save({"always_rules": ["B"]})
        profile = profile_service.restore("", 1)
        assert (profile.version, profile.data) == (3, {"always_rules": ["A"]})
        assert profile_service.list_versions().first().note == "restored version 1"

    def test_restore_unknown_version_is_not_found(self):
        with pytest.raises(ValueError, match="not found"):
            profile_service.restore("", 7)

    def test_unsaved_scope_is_empty_version_zero(self):
        profile = profile_service.get_profile("shop")
        assert (profile.pk, profile.version, profile.data) == (None, 0, {})

    def test_version_ids_and_record_use(self):
        instance = _save(PROFILE)
        overlay = _save({"tone": {"text": "Dry."}}, channel="shop")
        ids = profile_service.version_ids("shop")
        assert ids == [
            ProfileVersion.objects.get(profile=instance).pk,
            ProfileVersion.objects.get(profile=overlay).pk,
        ]
        task = EnrichmentTask.objects.create(type="t")
        profile_service.record_use(task=task, channel="shop")
        task.refresh_from_db()
        assert task.profile_versions == ids


class TestOverlay:
    def test_channel_sections_replace_instance_sections(self):
        _save(PROFILE)
        _save({"tone": {"text": "Dry."}, "brand": {}}, channel="shop")
        merged = profile_service.merged("shop")
        assert merged["tone"] == {"text": "Dry."}
        assert merged["brand"] == PROFILE["brand"]  # an empty overlay section does not blank the instance
        assert profile_service.merged("other") == PROFILE

    def test_list_sections_add_to_the_instance_and_the_overlay_wins_on_an_id(self):
        _save({"always_rules": ["A"], "facts": [{"id": "f1", "label": "L", "value": "1"}]})
        overlay_fact = {"id": "f1", "label": "L", "value": "2"}
        _save(
            {"always_rules": ["A", "B"], "facts": [overlay_fact, {"id": "f2", "label": "M", "value": "3"}]},
            channel="shop",
        )
        merged = profile_service.merged("shop")
        assert merged["always_rules"] == ["A", "B"]
        assert merged["facts"] == [overlay_fact, {"id": "f2", "label": "M", "value": "3"}]


class TestContext:
    def test_default_routing_per_kind(self):
        _save(PROFILE)
        instructions, data = profile_service.context_for(channel="", task_type="t", kind="pick")
        assert "## Brand" in instructions and "## Language" in instructions
        assert "## Tone of voice" not in instructions and "## Audience" not in instructions
        assert data == ""  # pick does not route facts

    def test_routing_by_task_type_wins_over_kind(self):
        _save({**PROFILE, "routing": {"pim.writer": ["seo"], "write": ["tone"]}})
        instructions, _ = profile_service.context_for(channel="", task_type="pim.writer", kind="write")
        assert "## SEO" in instructions and "## Tone of voice" not in instructions
        instructions, _ = profile_service.context_for(channel="", task_type="other", kind="write")
        assert "## Tone of voice" in instructions and "## SEO" not in instructions

    def test_conflict_order_and_always_rules(self):
        _save({**PROFILE, "routing": {"write": ["tone", "brand", "facts"]}})
        instructions, data = profile_service.context_for(channel="", task_type="t", kind="write")
        assert instructions.startswith(profile_context.CONFLICT_NOTE)
        assert instructions.index("Never promise delivery dates.") < instructions.index("## Brand")
        assert instructions.index("## Brand") < instructions.index("## Tone of voice")
        assert "- delve" in instructions and "- Made to order" in instructions

    def test_facts_are_a_data_table(self):
        _save(PROFILE)
        _, data = profile_service.context_for(channel="", task_type="t", kind="questions")
        lines = data.splitlines()
        assert lines[0] == "| id | label | value | source | verified_at |"
        assert "| f1 | Founded | 1999 | registry | 2026-01-02 |" in lines
        assert "Lodz \\| north" in data

    def test_language_section_lists_spellings_and_glossary(self):
        language = {**PROFILE["language"], "glossary": [{"term": "sofa", "translations": {"pl": "sofa"}}]}
        _save({"language": language})
        instructions, _ = profile_service.context_for(channel="", task_type="t", kind="pick")
        assert "write 'ecommerce', not 'e-commerce'" in instructions and "sofa (pl: sofa)" in instructions

    def test_empty_profile_gives_nothing(self):
        assert profile_service.context_for(channel="shop", task_type="t", kind="write") == ("", "")

    def test_cap_truncates_and_reports(self):
        facts = [{"id": f"f{i}", "label": "L", "value": "v" * 400} for i in range(20)]
        _save({"facts": facts, "brand": {"text": "b" * 6000}})
        instructions, data = profile_service.context_for(channel="", task_type="t", kind="write")
        assert len(data) < profile_context.CAP_CHARS  # facts kept whole: they win the budget
        assert len(instructions) + len(data) <= profile_context.CAP_CHARS + 100
        assert "[Company profile truncated:" in instructions

    def test_oversized_facts_keep_always_rules_and_whole_rows(self):
        facts = [{"id": f"f{i}", "label": "L", "value": "v" * 400} for i in range(40)]
        _save({"facts": facts, "brand": {"text": "b" * 100}, "always_rules": ["Never promise delivery dates."]})
        instructions, data = profile_service.context_for(channel="", task_type="t", kind="write")
        assert "Never promise delivery dates." in instructions
        assert profile_context.CONFLICT_NOTE in instructions
        assert data.startswith("| id | label | value | source | verified_at |")
        assert data.endswith(" |") and data.splitlines()[-1].startswith("| f")
        assert len(instructions) + len(data) <= profile_context.CAP_CHARS

    def test_whole_lines_cuts_at_a_line_end(self):
        assert profile_context.whole_lines("ab\ncd\nef", 6) == "ab\ncd"
        assert profile_context.whole_lines("abcdef", 3) == ""


class TestStatus:
    def test_empty_profile_is_incomplete(self):
        status = profile_service.status()
        assert status["complete"] is False
        assert [s["key"] for s in status["sections"]] == ["brand", "persona", "facts", "tone", "language"]

    def test_minimums(self):
        _save(PROFILE)
        assert profile_service.status()["complete"] is True

    def test_facts_need_three_verified(self):
        _save({**PROFILE, "facts": [*FACTS[:2], {"id": "f3", "label": "L", "value": "v"}]})
        sections = {s["key"]: s["complete"] for s in profile_service.status()["sections"]}
        assert sections["facts"] is False and sections["brand"] is True

    def test_tone_needs_three_lines_or_a_pattern(self):
        _save({"tone": {"text": "One.\nTwo.\n\nThree."}})
        assert {s["key"]: s["complete"] for s in profile_service.status()["sections"]}["tone"] is True
        _save({"tone": {"text": "One.\nTwo."}})
        assert {s["key"]: s["complete"] for s in profile_service.status()["sections"]}["tone"] is False

    def test_status_reads_the_merged_profile(self):
        _save({"brand": {"differentiators": ["Made to order"]}})
        _save({"persona": {"text": "Renovators."}}, channel="shop")
        sections = {s["key"]: s["complete"] for s in profile_service.status("shop")["sections"]}
        assert sections["brand"] and sections["persona"]
