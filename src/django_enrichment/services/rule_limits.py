# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Rule limits shared by proposal, worker and template rules (a leaf: no service imports)."""

MAX_RULES = 20
MAX_RULE_LENGTH = 200


def validate_rules(rules: list[str]) -> list[str]:
    """Strip, drop blanks, enforce the limits. Raises `ValueError`."""
    cleaned = [rule.strip() for rule in rules if rule.strip()]
    if len(cleaned) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} rules allowed")
    if any(len(rule) > MAX_RULE_LENGTH for rule in cleaned):
        raise ValueError(f"a rule may have at most {MAX_RULE_LENGTH} characters")
    return cleaned
