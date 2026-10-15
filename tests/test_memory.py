# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Memory (contract A4 § 3): recall order / expiry / category / budget, writers, promotion into the profile."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from django_enrichment.enums import MemoryKind, MemoryStatus
from django_enrichment.models import MemoryEntry
from django_enrichment.services import (
    memory_promotion,
    memory_service,
    profile_service,
    proposal_service,
    rules_service,
)

pytestmark = pytest.mark.django_db
ANSWER = MemoryKind.ANSWER.value
SEO = MemoryKind.SEO_BRIEF.value


def _entry(title: str, **over) -> MemoryEntry:
    fields = {"kind": SEO, "subject_module": "pim", "subject_ref": "SKU-1", "title": title, "text": f"{title} text"}
    return memory_service.remember(MemoryEntry(**{**fields, **over}))


def _recall(subject_ref: str = "SKU-1", budget: int = 3000, channel: str = "shop", kinds=(SEO,)) -> str:
    return memory_service.recall(
        channel=channel, subject_module="pim", subject_ref=subject_ref, kinds=list(kinds), budget_chars=budget
    )


class TestRecall:
    def test_newest_first(self):
        _entry("old")
        _entry("new")
        assert _recall() == "- [seo_brief] new: new text\n- [seo_brief] old: old text"

    def test_expired_dismissed_and_other_kinds_are_left_out(self):
        _entry("expired", expires_at=timezone.now() - timedelta(days=1))
        _entry("fresh", expires_at=timezone.now() + timedelta(days=1))
        _entry("dismissed", status=MemoryStatus.DISMISSED.value)
        _entry("answer", kind=ANSWER)
        assert _recall() == "- [seo_brief] fresh: fresh text"

    def test_instance_and_own_channel_only(self):
        _entry("instance")
        _entry("mine", channel="shop")
        _entry("theirs", channel="other")
        assert "theirs" not in _recall() and "mine" in _recall() and "instance" in _recall()

    def test_category_follows_the_subject(self, fake_adapter, monkeypatch):
        monkeypatch.setattr(fake_adapter, "category_of", lambda ref, *, channel=None: "category:sofas", raising=False)
        _entry("category brief", subject_ref="category:sofas")
        _entry("own brief")
        assert _recall().splitlines() == ["- [seo_brief] own brief: own brief text", _line("category brief")]

    def test_category_is_looked_up_in_the_recall_channel(self, fake_adapter, monkeypatch):
        asked: list[tuple[str, str | None]] = []
        monkeypatch.setattr(
            fake_adapter, "category_of", lambda ref, *, channel=None: asked.append((ref, channel)), raising=False
        )
        _recall()
        assert asked == [("SKU-1", "shop")]

    def test_no_category_without_the_extra(self, fake_adapter):
        _entry("category brief", subject_ref="category:sofas")
        assert _recall() == ""

    def test_budget_keeps_whole_lines(self):
        _entry("one")
        _entry("two")
        line = "- [seo_brief] two: two text"
        assert _recall(budget=len(line) + 5) == line
        assert _recall(budget=10) == line[:10]

    def test_data_shown_when_there_is_no_text(self):
        _entry("brief", text="", data={"primary": "linen sofa"})
        assert _recall() == '- [seo_brief] brief: {"primary": "linen sofa"}'

    def test_remember_cuts_title_and_text(self):
        entry = _entry("t" * 300, text="x" * 5000)
        assert (len(entry.title), len(entry.text)) == (200, 4000)

    def test_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="unknown memory kind"):
            _entry("x", kind="gossip")


def _line(title: str) -> str:
    return f"- [seo_brief] {title}: {title} text"


class TestWriters:
    def test_answers_become_general_entries(self, admin_user):
        run = SimpleNamespace(uid="abc", channel="shop", created_by=admin_user)
        questions = [
            {"id": "years", "question": "Since when?"},
            {"id": "tone", "question": "Tone?"},
            {"id": "extra", "question": "Else?"},
        ]
        value = {"questions": questions, "answers": {"years": "Since 1999", "tone": "warm", "extra": " "}}
        memory_service.record_answers(run=run, step="ask", value=value)
        entries = MemoryEntry.objects.order_by("pk")
        assert [(e.title, e.text, e.source, e.channel, e.subject_ref) for e in entries] == [
            ("Since when?", "Since 1999", "run:abc/ask", "shop", ""),
            ("Tone?", "warm", "run:abc/ask", "shop", ""),
        ]
        recalled = memory_service.recall(
            channel="shop", subject_module="", subject_ref="", kinds=[ANSWER], budget_chars=1000
        )
        assert "Since when?: Since 1999" in recalled

    def test_rule_promotion_to_profile_writes_a_suggestion(self, fake_adapter):
        proposal = proposal_service.intake(
            target_module="pim",
            subject_ref="SKU-1",
            target_kind="attribute_value",
            target_locator={"channel": "shop", "language": "pl", "feature_idx": "description"},
            proposed_value={"text": "Copy."},
        )
        assert rules_service.promote(proposal, " No emojis. ", target="profile") == ["No emojis."]
        entry = MemoryEntry.objects.get()
        assert (entry.kind, entry.channel, entry.source) == ("rule_suggestion", "shop", f"proposal:{proposal.pk}")
        assert profile_service.get_profile("shop").version == 0  # the profile itself is untouched


class TestStatus:
    def test_dismiss_and_reactivate(self):
        entry = _entry("x")
        assert memory_service.set_status(entry, "dismissed").status == "dismissed"
        assert memory_service.set_status(entry, "active").status == "active"

    def test_promoted_is_not_settable(self):
        entry = _entry("x")
        with pytest.raises(ValueError, match="status must be"):
            memory_service.set_status(entry, "promoted")
        entry.status = "promoted"
        with pytest.raises(ValueError, match="promoted entry"):
            memory_service.set_status(entry, "dismissed")

    def test_list_filters(self):
        _entry("a", channel="shop")
        _entry("b", kind=ANSWER)
        assert [e.title for e in memory_service.list_entries(channel="shop")] == ["a"]
        assert [e.title for e in memory_service.list_entries(kind=ANSWER)] == ["b"]


def _suggestion(kind: str = "rule_suggestion", **over) -> MemoryEntry:
    fields = {"kind": kind, "title": "No emojis.", "text": "No emojis.", "source": "proposal:1"}
    return memory_service.remember(MemoryEntry(**{**fields, **over}))


class TestPromotion:
    def test_rule_goes_to_always_rules(self, admin_user):
        profile_service.save_profile(channel="", data={"always_rules": ["Be kind."]})
        entry = _suggestion()
        profile = memory_promotion.promote(entry, user=admin_user)
        assert profile.data["always_rules"] == ["Be kind.", "No emojis."]
        assert (profile.version, entry.status) == (2, "promoted")
        assert profile_service.list_versions().first().note == f"promoted memory {entry.pk}"

    def test_rule_with_a_pattern_goes_to_lint_rules(self):
        entry = _suggestion(data={"pattern": "e-commerce", "flags": "i", "do": "ecommerce"}, channel="shop")
        profile = memory_promotion.promote(entry, target="rules")
        assert profile.channel == "shop"
        assert profile.data["rules"] == [
            {
                "id": f"m{entry.pk}",
                "pattern": "e-commerce",
                "message": "No emojis.",
                "flags": "i",
                "do": "ecommerce",
                "active": True,
            }
        ]

    def test_lint_rule_needs_a_pattern(self):
        with pytest.raises(ValueError, match="pattern"):
            memory_promotion.promote(_suggestion(), target="rules")

    def test_fact_goes_to_facts_verified_today(self):
        entry = _suggestion("fact_suggestion", title="Warranty", text="5 years", data={"source": "manual"})
        profile = memory_promotion.promote(entry)
        assert profile.data["facts"] == [
            {
                "id": f"m{entry.pk}",
                "label": "Warranty",
                "value": "5 years",
                "source": "manual",
                "verified_at": timezone.localdate().isoformat(),
            }
        ]

    def test_promotion_into_a_channel_overlay_keeps_the_instance_lists(self):
        instance_fact = {"id": "warranty", "label": "Warranty", "value": "2 years"}
        profile_service.save_profile(channel="", data={"facts": [instance_fact], "always_rules": ["Be kind."]})
        memory_promotion.promote(_suggestion("fact_suggestion", title="Delivery", text="48 h", channel="shop"))
        memory_promotion.promote(_suggestion(channel="shop"))
        merged = profile_service.merged("shop")
        assert [fact["id"] for fact in merged["facts"]][0] == "warranty"
        assert len(merged["facts"]) == 2
        assert merged["always_rules"] == ["Be kind.", "No emojis."]

    @pytest.mark.parametrize(
        ("kind", "target", "message"),
        [("answer", None, "cannot be promoted"), ("fact_suggestion", "always_rules", "promotes to facts")],
    )
    def test_wrong_kind_or_target(self, kind, target, message):
        with pytest.raises(ValueError, match=message):
            memory_promotion.promote(_suggestion(kind), target=target)

    def test_only_active_entries(self):
        entry = _suggestion(status="dismissed")
        with pytest.raises(ValueError, match="only active"):
            memory_promotion.promote(entry)
        assert profile_service.get_profile().version == 0

    def test_invalid_result_rolls_back(self):
        entry = _suggestion(data={"pattern": "(["})
        with pytest.raises(profile_service.ProfileValidationError):
            memory_promotion.promote(entry, target="rules")
        entry.refresh_from_db()
        assert entry.status == "active"
