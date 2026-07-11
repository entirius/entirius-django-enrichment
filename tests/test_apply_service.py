# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Architectural guards for apply_service (etap-04).

apply_service is the bus's single write choke point. Its whole point is module-agnostic routing:
it must dispatch by `proposal.target_module` through the adapter registry, never with a hardcoded
`if target_module == "pim"` branch. Drift / applied / apply_many behaviour is covered in
`test_proposal_service.py`; here we pin the routing contract itself.
"""

import inspect
import io
import tokenize

import pytest
from django.core.exceptions import ObjectDoesNotExist

from django_enrichment.enums import ProposalStatus
from django_enrichment.models import ContentProposal
from django_enrichment.services import apply_service

_IGNORED_TOKENS = frozenset(
    {tokenize.COMMENT, tokenize.STRING, tokenize.FSTRING_START, tokenize.FSTRING_MIDDLE, tokenize.FSTRING_END}
)


def _code_tokens(module) -> str:
    """Executable tokens of a module, lowercased — strings and comments stripped.

    Lets the guard assert on real code, not on a docstring that quotes the forbidden pattern
    precisely to forbid it.
    """
    src = inspect.getsource(module)
    names = [
        tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline) if tok.type not in _IGNORED_TOKENS
    ]
    return " ".join(names).lower()


def test_apply_service_has_no_hardcoded_module_branch():
    """The bus must not special-case any source module — routing is registry-only."""
    code = _code_tokens(apply_service)

    assert "pim" not in code  # no source-module literal in executable code
    assert "get_adapter" in code  # dispatch goes through the registry


@pytest.mark.django_db
def test_apply_routes_by_target_module_unregistered_raises(fake_adapter):
    """A proposal whose target_module has no registered adapter fails at the registry lookup.

    Proves there is no hardcoded fallback: only "pim" is registered (by the fixture), so a
    "contentdb" proposal raises rather than being silently handled by some default path.
    """
    proposal = ContentProposal.objects.create(
        target_module="contentdb",
        subject_ref="X",
        target_kind="attribute_value",
        target_locator={"k": 1},
        proposed_value={"text": "v"},
        current_snapshot={},
        status=ProposalStatus.PENDING.value,
    )

    with pytest.raises(ValueError, match="no enrichment adapter"):
        apply_service.apply(proposal)


@pytest.mark.django_db
def test_apply_missing_target_raises_readable_valueerror_not_500(fake_adapter, monkeypatch):
    """A vanished target (deleted SKU / channel) must surface as a readable ValueError, never a bare
    `ObjectDoesNotExist` bubbling up as a 500. Adapters raise Django's `DoesNotExist` when the target
    is gone; the bus translates it into a 400-mappable message that names the SKU and channel.
    """
    proposal = ContentProposal.objects.create(
        target_module="pim",
        subject_ref="GONE-SKU",
        target_kind="attribute_value",
        target_locator={"channel": "default", "language": "pl", "feature_idx": "description"},
        proposed_value={"text": "v"},
        current_snapshot={},
        status=ProposalStatus.PENDING.value,
    )

    def _boom(_proposal):
        raise ObjectDoesNotExist("Channel matching query does not exist.")

    monkeypatch.setattr(fake_adapter, "apply", _boom)

    with pytest.raises(ValueError) as exc_info:
        apply_service.apply(proposal)

    msg = str(exc_info.value)
    assert "GONE-SKU" in msg and "default" in msg  # readable: names the SKU + channel
    assert not isinstance(exc_info.value, ObjectDoesNotExist)  # translated, not the raw 500 cause
    proposal.refresh_from_db()
    assert proposal.status == ProposalStatus.PENDING.value  # nothing written, proposal left intact
