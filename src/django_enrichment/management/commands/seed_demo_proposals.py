# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Seed the enrichment review queue with realistic demo proposals (text + picture).

Dev/demo only. Creates a couple of `EnrichmentTask`s and a batch of PENDING `ContentProposal`s over
the standard seeded catalogue (`ENT-S00x`) so the CMS review queue has something to look at —
descriptions, SEO copy, and main-image replacements with generated placeholder images. Idempotent
via `external_ref`: re-running supersedes/skips, never duplicates. Targets real products through the
registered adapter, so run it after the catalogue is imported (the seed pipeline does).
"""

import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand

from django_enrichment.enums import TaskStatus
from django_enrichment.models import EnrichmentTask
from django_enrichment.services import proposal_service

_CHANNEL = "default-europe"

# (sku, language, feature_idx, source, confidence, proposed_text)
_TEXT_PROPOSALS = [
    (
        "ENT-S001",
        "en",
        "description",
        "n8n:describe-v2",
        "0.93",
        "Built for everyday use, the ENT-S001 pairs hard-wearing materials with a clean, modern finish. "
        "Thoughtful details and a comfortable feel make it an easy pick whether you are upgrading or buying your first.",
    ),
    (
        "ENT-S001",
        "en",
        "short_description",
        "n8n:describe-v2",
        "0.90",
        "Durable, modern, and comfortable — a reliable everyday choice.",
    ),
    (
        "ENT-S002",
        "en",
        "description",
        "n8n:describe-v2",
        "0.88",
        "The ENT-S002 keeps things simple where it counts: a sturdy build, a balanced design, and finishing "
        "that holds up to daily handling. A dependable option that does its job without fuss.",
    ),
    ("ENT-S002", "en", "meta_title", "n8n:seo-pass", "0.81", "ENT-S002 — Durable Everyday Design | Shop Now"),
    (
        "ENT-S002",
        "en",
        "meta_description",
        "n8n:seo-pass",
        "0.79",
        "Discover the ENT-S002: a sturdy, well-balanced design made for daily use. Free returns and fast shipping.",
    ),
    (
        "ENT-S003",
        "en",
        "description",
        "n8n:describe-v2",
        "0.95",
        "Refined and ready for anything, the ENT-S003 brings together a premium feel and practical design. "
        "Every detail is tuned for comfort and longevity, so it keeps performing long after the first use.",
    ),
    (
        "ENT-S004",
        "en",
        "description",
        "manual:editor",
        "0.99",
        "A standout in its range, the ENT-S004 combines a confident look with materials chosen for the long haul. "
        "Easy to live with, hard to wear out.",
    ),
    (
        "ENT-S005",
        "en",
        "short_description",
        "n8n:describe-v2",
        "0.86",
        "Clean lines, solid build, all-day comfort — the ENT-S005 keeps it effortless.",
    ),
    (
        "ENT-S006",
        "en",
        "meta_description",
        "n8n:seo-pass",
        "0.77",
        "Meet the ENT-S006: modern design, lasting quality, everyday value. In stock now with free returns.",
    ),
    (
        "ENT-S001",
        "en",
        "meta_title",
        "n8n:seo-pass",
        "0.83",
        "ENT-S001 — Modern, Durable, Comfortable | Official Store",
    ),
]

# (sku, alt_text, source, confidence, fill_colour)
_MEDIA_PROPOSALS = [
    ("ENT-S001", "Front view of the ENT-S001 on a neutral background", "n8n:media-refresh", "0.84", (37, 99, 235)),
    ("ENT-S002", "ENT-S002 studio shot, three-quarter angle", "n8n:media-refresh", "0.80", (22, 163, 74)),
    ("ENT-S004", "ENT-S004 detail close-up", "n8n:media-refresh", "0.88", (217, 70, 70)),
    ("ENT-S006", "ENT-S006 packshot, white background", "n8n:media-refresh", "0.75", (147, 51, 234)),
]


def _demo_image(label: str, colour: tuple[int, int, int]) -> SimpleUploadedFile:
    """A labelled placeholder PNG so the 'after' image is visually distinct in the diff view."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 600), color=colour)
    draw = ImageDraw.Draw(image)
    draw.text((40, 285), f"{label}\n(enriched image)", fill="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return SimpleUploadedFile(f"{label}-enriched.png", buffer.read(), content_type="image/png")


class Command(BaseCommand):
    help = "Seed demo enrichment proposals (text + picture) onto the review queue"

    def handle(self, *args, **options):
        describe = EnrichmentTask.objects.get_or_create(
            batch_key="demo:describe-seo",
            defaults={
                "type": "describe",
                "status": TaskStatus.IN_PROGRESS.value,
                "scope_spec": {"module": "pim"},
                "params": {"note": "demo seed — descriptions + SEO"},
            },
        )[0]
        media = EnrichmentTask.objects.get_or_create(
            batch_key="demo:media-refresh",
            defaults={
                "type": "media",
                "status": TaskStatus.IN_PROGRESS.value,
                "scope_spec": {"module": "pim"},
                "params": {"note": "demo seed — main image refresh"},
            },
        )[0]

        text_count = 0
        for i, (sku, language, feature_idx, source, confidence, text) in enumerate(_TEXT_PROPOSALS):
            proposal_service.intake(
                target_module="pim",
                subject_ref=sku,
                target_kind="text",
                target_locator={"channel": _CHANNEL, "language": language, "feature_idx": feature_idx},
                proposed_value={"text": text},
                task=describe,
                subject_label=sku,
                source=source,
                confidence=Decimal(confidence),
                external_ref=f"demo-text-{i}",
            )
            text_count += 1

        media_count = 0
        for i, (sku, alt, source, confidence, colour) in enumerate(_MEDIA_PROPOSALS):
            proposal_service.intake_media(
                file=_demo_image(sku, colour),
                target_module="pim",
                subject_ref=sku,
                target_locator={"channel": _CHANNEL},
                proposed_value={"op": "replace_main", "alt_t9n": {"en": alt}},
                task=media,
                subject_label=sku,
                source=source,
                confidence=Decimal(confidence),
                external_ref=f"demo-media-{i}",
            )
            media_count += 1

        if options["verbosity"] > 0:
            self.stdout.write(f"Seeded {text_count} text + {media_count} picture demo proposals (pending).")
