"""Rule-based partition pre-filter.

A cheap, deterministic gate in front of the LLM partition validator. It
classifies the obvious shapes — direct yes/no complements, explicit
negations, integer-push over/unders, win/loss without a draw, asian
handicap overlap, etc. — and defers the rest as UNKNOWN so the LLM only
sees the genuinely ambiguous pairs.

Architectural contract (see docs/architecture.md):
    The partition validator as a whole must reach > 99% precision on the
    VALID label. This pre-filter is the first layer of that guarantee:
    when it returns VALID it MUST be correct, because a false positive
    becomes an unhedged bet downstream. INVALID errors are softer (the
    risk is a missed arb, not a financial loss).

Rules are cascaded in the order they appear in `_RULES`. The first rule
to return a non-None result wins; the remaining rules are not consulted.
When no rule fires the verdict is UNKNOWN and the caller is expected to
escalate to the LLM validator.

This module is intentionally additive: new rules are appended to `_RULES`
with their own unit tests, and the fixture-driven evaluation in
tests/unit/test_partition_filter_eval.py asserts that adding rules never
introduces a mislabel.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class Verdict(Enum):
    """Three-state outcome of the pre-filter."""

    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MatchContext:
    """Context the pre-filter needs to disambiguate stage-dependent rules.

    The win/loss trap, for example, depends on whether a draw is possible at
    the relevant scope: in non-knockout matches a draw at 90 minutes is a
    final outcome; in knockout matches the same 90-minute draw goes to extra
    time, but for a 90-minute-scoped bet the draw is still a missing
    outcome.
    """

    competition: str
    stage: str
    is_knockout: bool


# A rule returns (verdict, reason) when it fires and None to defer.
Rule = Callable[[str, str, MatchContext], tuple[Verdict, str] | None]


def classify(desc_a: str, desc_b: str, context: MatchContext) -> tuple[Verdict, str]:
    """Classify a partition candidate via the rule cascade.

    Returns:
        (verdict, reason). When `verdict == UNKNOWN` the reason is the
        sentinel string `"no rule matched"` and the caller should escalate
        to the LLM validator. When `verdict` is VALID or INVALID the reason
        names the rule that fired so the audit log in
        `partition_validations` can record the decision provenance.
    """
    for rule in _RULES:
        result = rule(desc_a, desc_b, context)
        if result is not None:
            return result
    return (Verdict.UNKNOWN, "no rule matched")


# ---- Text normalization ----------------------------------------------------


def _normalize(text: str) -> str:
    """Lower-case, strip diacritics, collapse whitespace.

    Preserves dots between digits (so '2.5' stays '2.5' for the over/under
    rules to consume) while turning every other punctuation cluster into a
    single space. Decimal commas (Spanish convention) are coerced to dots
    first so '2,5' becomes '2.5'.
    """
    decomposed = unicodedata.normalize("NFD", text)
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = no_accents.lower()
    # 1,5 → 1.5 so the dot-preservation step below handles both conventions.
    lowered = re.sub(r"(\d),(\d)", r"\1.\2", lowered)
    # Replace any run of non-alphanumeric-and-not-dot with a space.
    cleaned = re.sub(r"[^a-z0-9.]+", " ", lowered)
    # Drop stray dots that aren't between digits ('btts.' → 'btts ').
    cleaned = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _tokens(text: str) -> list[str]:
    return _normalize(text).split()


# ---- Individual rules ------------------------------------------------------


def _rule_yes_no_complement(
    desc_a: str, desc_b: str, context: MatchContext
) -> tuple[Verdict, str] | None:
    """A vs B differs only by an explicit 'Sí/Yes' on one side and 'No' on
    the other.

    Catches the canonical clean-binary shape used by BTTS, anytime scorer,
    penalty awarded, red card, and similar yes/no markets — including the
    case where the yes/no token sits in the middle of the phrase with a
    common parenthetical tail (e.g. "Ambos marcan: Sí (todo el partido)").

    Requires exactly one occurrence of each token to avoid pathological
    matches like "Marca primero, no segundo" / "Marca primero, sí segundo".
    """
    tokens_a = _tokens(desc_a)
    tokens_b = _tokens(desc_b)
    for yes_token in ("si", "yes"):
        if _maybe_yes_no_pair(tokens_a, tokens_b, yes_token):
            return (Verdict.VALID, f"yes/no complement ({yes_token}/no)")
        if _maybe_yes_no_pair(tokens_b, tokens_a, yes_token):
            return (Verdict.VALID, f"yes/no complement ({yes_token}/no)")
    return None


# Tokens that turn a phrase into a compound proposition. When one of these
# is in the common prefix of a yes/no pair, the Sí/No likely scopes to only
# part of the proposition (see fixture case `result_btts_combo_missing`:
# 'Boca gana y BTTS Sí' / 'Boca gana y BTTS No' covers only Boca-wins
# outcomes, not the full space). The rule defers in that case.
_COMPOUND_CONNECTIVES = frozenset({"y", "and", "o", "or"})


def _maybe_yes_no_pair(yes_tokens: list[str], no_tokens: list[str], yes: str) -> bool:
    if yes_tokens.count(yes) != 1 or no_tokens.count("no") != 1:
        return False
    stripped_yes = [t for t in yes_tokens if t != yes]
    stripped_no = [t for t in no_tokens if t != "no"]
    if not stripped_yes or stripped_yes != stripped_no:
        return False
    return not any(t in _COMPOUND_CONNECTIVES for t in stripped_yes)


def _rule_negation_complement(
    desc_a: str, desc_b: str, context: MatchContext
) -> tuple[Verdict, str] | None:
    """A vs B where B is A with 'no' inserted (or vice versa) and otherwise
    identical token-by-token.

    Catches direct negation patterns: "Boca gana al medio tiempo" vs "Boca
    no gana al medio tiempo", "Empate al final" vs "No empate al final",
    "Boca gana y BTTS Sí" vs "No (Boca gana y BTTS Sí)".

    Sequence-strict (not multiset) to avoid false positives from rephrasings
    that share the same word bag but mean different things.
    """
    tokens_a = _tokens(desc_a)
    tokens_b = _tokens(desc_b)
    if _is_negation_of(tokens_b, tokens_a) or _is_negation_of(tokens_a, tokens_b):
        return (Verdict.VALID, "negation complement")
    return None


def _is_negation_of(negated: list[str], base: list[str]) -> bool:
    """True iff `negated` equals `base` with exactly one 'no' inserted and no
    'no' present in `base`."""
    if "no" in base or negated.count("no") != 1:
        return False
    stripped = [t for t in negated if t != "no"]
    return bool(stripped) and stripped == base


# Cascade order: most specific first. Cheap, narrow rules go before broader
# ones so the broader rules don't shadow a more informative match.
_RULES: list[Rule] = [
    _rule_yes_no_complement,
    _rule_negation_complement,
]
