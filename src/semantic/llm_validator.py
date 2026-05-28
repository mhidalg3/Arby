"""LLM-based partition validator.

The semantic-layer fallback for partition cases the rule-based pre-filter
defers as UNKNOWN. Wraps the Claude API behind a small, typed surface:
takes two bet descriptions plus match context, returns a `verdict`/
`confidence`/`reasoning`/`trap_pattern` record.

Design choices (anchored to docs/architecture.md):

- **Strict tool use** (`tool_choice` forces `report_partition_verdict`) is
  the structured-output mechanism — guarantees a parseable response shape
  and lets the model commit to a single, validated decision rather than
  free-form text we'd have to regex.
- **Adaptive thinking** is on by default. Partition validation is a
  reasoning task (scope checks, push detection, AH overlap analysis); a
  shallow forward pass produces fragile results.
- **Prompt caching** is enabled on the system prompt. Opus 4.7's cache
  minimum is 4096 tokens, so the prompt is deliberately comprehensive —
  the trap taxonomy + worked examples both teach the classifier and clear
  the floor. Without enough length, `cache_read_input_tokens` would stay
  at zero and we'd pay full input price on every call.
- **Precision on VALID is the safety property**. The system prompt is
  explicit about the cost asymmetry: a false VALID downstream becomes an
  unhedged bet; a false INVALID is just a missed arb. When uncertain, the
  model is instructed to return INVALID.

This module does not call out to the rule-based pre-filter. A higher-level
entry point composes the two (pre-filter first, escalate UNKNOWN to here).
That composition is the next slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import anthropic
import structlog
from anthropic.types import (
    MessageParam,
    OutputConfigParam,
    TextBlockParam,
    ThinkingConfigAdaptiveParam,
    ToolChoiceToolParam,
    ToolParam,
)

from src.semantic.partition_filter import MatchContext, Verdict

logger = structlog.get_logger(__name__)

# The tool the model is forced to invoke. Strict mode means every property
# below MUST appear in `required` and `additionalProperties` is false — the
# response shape is therefore guaranteed parseable.
TOOL_NAME = "report_partition_verdict"

PARTITION_TOOL: ToolParam = {
    "name": TOOL_NAME,
    "description": (
        "Report whether the two candidate bet descriptions form a valid "
        "partition of the match outcome space. A valid partition requires "
        "both mutual exclusivity (the two bets cannot both win on the same "
        "outcome) and exhaustiveness (every possible outcome makes exactly "
        "one of the two bets win)."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["valid", "invalid"],
                "description": (
                    "'valid' iff the two bets form a strict partition. "
                    "When uncertain, prefer 'invalid' — the cost of a "
                    "false 'valid' is a real financial loss; the cost of "
                    "a false 'invalid' is only a missed arbitrage."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": (
                    "Confidence in the verdict. Use 'low' freely — the "
                    "calling layer can route low-confidence verdicts for "
                    "human review."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "One or two concise sentences explaining the decision. "
                    "Name the specific outcome that breaks the partition "
                    "if invalid (e.g., 'misses the draw outcome', 'pushes "
                    "on exactly 2 goals')."
                ),
            },
            "trap_pattern": {
                "type": "string",
                "description": (
                    "If invalid, the named trap pattern (e.g., "
                    "'win_loss_without_draw', 'integer_push', "
                    "'asian_handicap_overlap', 'scope_mismatch', "
                    "'double_chance_overlap', 'goalscorer_overlap', "
                    "'combo_missing_outcomes', 'to_qualify_scope_mismatch'). "
                    "Use 'none' when verdict is 'valid'."
                ),
            },
        },
        "required": ["verdict", "confidence", "reasoning", "trap_pattern"],
        "additionalProperties": False,
    },
}


# The system prompt. Deliberately long: it carries the trap taxonomy that
# the rule-based filter cannot encode, plus worked examples. Length is also
# the lever for prompt caching — at ≥ 4096 tokens, the cache fires on Opus
# 4.7 and subsequent calls pay ~0.1× input price on the cached portion.
SYSTEM_PROMPT = """\
You are a binary classifier inside an Argentine soccer arbitrage system.

Given two candidate bet descriptions and a match context, decide whether
the two bets form a VALID PARTITION of the match's outcome space.

# Definition

A pair (A, B) is a valid partition iff for every possible outcome of the
match, exactly one of A or B wins. Equivalently:

  - Mutually exclusive: P(A wins AND B wins) = 0
  - Exhaustive:         P(A wins OR  B wins) = 1
  - No pushes/refunds:  every outcome decisively wins or loses each leg

Both conditions must hold. Missing either makes the pair INVALID.

# The safety property (critical)

Precision on VALID is what protects real money downstream. A false VALID
verdict here becomes an unhedged bet — the system places stakes on both
legs expecting one to win, and if neither (or both) wins on the actual
outcome, the capital is lost or doubly exposed. A false INVALID verdict
only suppresses a real arbitrage opportunity. We prefer many missed arbs
over one unhedged bet.

When the analysis is uncertain, the answer is INVALID. Do not stretch to
classify a pair as valid because "it usually is" — the cost is asymmetric.

# Output format

Always invoke the `report_partition_verdict` tool. The tool fields:

  - verdict: 'valid' | 'invalid'
  - confidence: 'high' | 'medium' | 'low'   — use 'low' freely
  - reasoning: 1–2 sentences. If invalid, name the specific outcome that
    breaks the partition (e.g., "misses the draw at full time", "pushes
    on exactly 2 goals", "both contain Boca-wins outcome").
  - trap_pattern: trap name when invalid; 'none' when valid.

# Trap taxonomy

The patterns below are the dominant failure modes. Most invalid pairs map
to one of these. The trap name in parentheses is the value to put in
`trap_pattern`.

## 1. Win/loss without draw  (`win_loss_without_draw`)

"Team A wins" vs "Team B wins" misses the DRAW outcome. In Argentine
soccer almost every match can draw at full time:

  - In regular season and group stages, the draw is the final outcome.
  - In knockout matches, a 90-minute draw is still possible — the tie
    then continues into extra time and penalties. If the bet scope is
    "90 minutes" or "regular time", the draw is the missing outcome.
  - Only in knockout matches scoped to "to advance" or "to qualify" is
    "Team A wins (tie)" vs "Team B wins (tie)" a clean binary — exactly
    one side advances after ET / penalties.

Examples:

  INVALID — "Gana Boca Juniors" vs "Gana River Plate" (Liga Profesional
    regular season). Both teams losing the same match is possible via
    the draw outcome. trap: win_loss_without_draw.

  INVALID — "Gana Boca en tiempo regular" vs "Gana Palmeiras en tiempo
    regular" (Copa Libertadores R16). The 90-minute draw is missing
    even though the tie itself has a winner. trap: win_loss_without_draw.

  VALID — "Avanza River (eliminatoria)" vs "Eliminado River
    (eliminatoria)" (Copa Argentina QF). One side definitely advances
    after ET / pens; clean binary.

## 2. Over/Under integer push  (`integer_push`)

"Over N" vs "Under N" with N an integer creates a PUSH on exactly N
goals/corners/cards — bets are refunded, not won. Not a strict partition.

  INVALID — "Más de 2 goles" vs "Menos de 2 goles" — pushes at exactly
    2. trap: integer_push.

  INVALID — "Más de 10 córners" vs "Menos de 10 córners" — pushes at
    exactly 10.

  VALID — "Más de 2.5 goles" vs "Menos de 2.5 goles" — half-line cannot
    push; every match has either ≥3 or ≤2 goals.

  VALID — "Over 0.5" vs "Under 0.5" — half line at zero is also clean.

The half-line rule generalizes: any N.5 line over a discrete quantity
(goals, corners, cards) is a clean binary; any integer N line is a push.

## 3. Asian Handicap on the same team  (`asian_handicap_overlap`)

"Team X AH -L" vs "Team X AH +L" both bet on the same team; the win
conditions overlap. E.g. "Boca -0.5" requires Boca wins; "Boca +0.5"
requires Boca wins OR draws — both fire when Boca wins.

  INVALID — "Boca AH -0.5" vs "Boca AH +0.5" — overlap on Boca wins.
    trap: asian_handicap_overlap.

The clean complement at line L is "TeamA -L" vs "OpponentTeam +L" — the
two bets are on different sides of the same line.

  VALID — "River -1.5" vs "Boca +1.5" — River wins by ≥2 vs everything
    else.

## 4. Asian Handicap integer line  (`asian_handicap_push`)

"Team A -N" vs "Team B +N" with N an integer pushes on exactly N-goal
wins. Same logic as over/under integer push.

  INVALID — "Estudiantes -1" vs "Banfield +1" — pushes when Estudiantes
    wins by exactly 1. trap: asian_handicap_push.

## 5. Asian Handicap quarter line  (`asian_handicap_quarter`)

Lines ending in .25 or .75 (split lines) internally divide the stake
across two half-lines — outcomes can produce half-wins or half-losses,
so the bet doesn't cleanly win or lose. Not a strict partition.

  INVALID — "Newell's -0.25" vs "Rosario Central +0.25". trap:
    asian_handicap_quarter.

  INVALID — "Talleres -0.75" vs "Huracán +0.75". trap:
    asian_handicap_quarter.

## 6. Double-chance overlap  (`double_chance_overlap`)

Double-chance markets group two of the three 1X2 outcomes:
  - 1X = home wins OR draw
  - 12 = home wins OR away wins (no draw)
  - X2 = draw OR away wins

Two double-chance bets overlap whenever they share an outcome:
  - 1X ∩ 12 = home wins      → overlap
  - 1X ∩ X2 = draw           → overlap
  - 12 ∩ X2 = away wins      → overlap

Valid double-chance partitions: single outcome vs the other two,
collapsed to 2-way form:
  - 1 vs X2        (home vs draw-or-away)
  - X vs 12        (draw vs no-draw)
  - 2 vs 1X        (away vs home-or-draw)

Examples:

  INVALID — "Doble oportunidad 1X" vs "Doble oportunidad X2" — both
    contain the draw. trap: double_chance_overlap.

  VALID — "Gana Boca" vs "Empate o gana River" — 1 vs X2.

## 7. Goalscorer overlap  (`goalscorer_overlap`)

"Player A scores" vs "Player B scores" — both can happen (overlap on
matches where both score), and neither needs to happen (gap on 0-0 or
goals only by other players). Not a partition.

The clean goalscorer binary is on a single player: "Player A scores
yes" vs "Player A scores no". The 'no' side covers the 0-0 case and
all matches where only other players score.

Examples:

  INVALID — "Marca Lautaro Martínez" vs "Marca Ángel Di María" — two
    players. trap: goalscorer_overlap.

  VALID — "Cavani marca: Sí" vs "Cavani marca: No" — single-player
    binary.

  INVALID — "Primer goleador: Tevez" vs "Primer goleador: Villa" —
    first goal could be a third player or no goal at all.

## 8. Scope mismatch  (`scope_mismatch`)

Two bets that look complementary but reference different time scopes
(half vs full match, this leg vs full tie, 90-min vs ET-included) do
not form a partition. The mismatch creates both overlap and gaps.

Examples:

  INVALID — "Más de 1.5 goles en el primer tiempo" vs "Menos de 1.5
    goles totales (FT)". A match with 0 goals in 1H but 2+ in 2H is
    missed; a match with 2+ in 1H is double-counted. trap:
    scope_mismatch.

  INVALID — "Ambos marcan 1H: Sí" vs "Ambos marcan partido completo:
    No". 1H both-score implies FT both-score, so the FT-No leg
    excludes the 1H-Yes set. Combined they miss many real outcomes.

  INVALID — "Empate al medio tiempo" vs "Empate al final" — HT-draw
    and FT-draw are independent; many matches have both, many neither.

## 9. Combo with shared conjunct  (`combo_missing_outcomes`)

A pair where each bet is a conjunction (X AND Y) and only one conjunct
differs may look like a clean Y vs ¬Y binary but actually covers only
the X subspace:

  INVALID — "Boca gana y BTTS Sí" vs "Boca gana y BTTS No". Both
    require Boca to win. Together they cover Boca-wins outcomes only;
    draws and away wins are entirely missed. trap:
    combo_missing_outcomes.

  VALID — "Boca gana y BTTS Sí" vs "No (Boca gana y BTTS Sí)". The
    second leg is the explicit negation of the whole conjunction;
    these together cover everything.

## 10. To-qualify vs win-this-leg  (`to_qualify_scope_mismatch`)

In a two-leg knockout tie, "Team X advances on aggregate" and "Team X
wins this leg" are different scopes. A team can lose this leg and
still advance on aggregate, or win this leg and still be eliminated.

  INVALID — "Clasifica Independiente (eliminatoria)" vs "Gana
    Independiente este partido (90 min)". trap:
    to_qualify_scope_mismatch.

# Match context interpretation

The context object carries `competition`, `stage`, `is_knockout`. Use
these to resolve scope ambiguity:

  - is_knockout: false → the draw is a final outcome at 90 minutes.
    Any "Team A wins" / "Team B wins" pair is invalid.
  - is_knockout: true → the tie has a definite winner via ET/pens, but
    individual-match-scoped bets ("wins this leg", "90 minutes")
    still allow the 90-minute draw.

The competition string (Liga Profesional, Copa Libertadores, etc.) is
contextual color; the substantive logic depends on is_knockout and on
the bet descriptions themselves naming a scope (e.g., "tiempo regular",
"eliminatoria", "primer tiempo").

# Wording, language, synonyms

Bet descriptions appear in Spanish or English; common synonyms include:

  - "Ambos equipos marcan" = "Both teams to score" = BTTS
  - "Más / Menos de N goles" = "Over / Under N goals"
  - "Gana X" = "X wins"
  - "Empate" = "Draw"
  - "Doble oportunidad" / "1X" / "12" / "X2" = double chance
  - "Sí / No" = "Yes / No"
  - "Avanza / Eliminado / Clasifica" = "Advances / Eliminated / Qualifies"
  - "Tiempo regular" = 90 minutes, no ET
  - "AH" or "handicap" = Asian Handicap
  - "Córners" = corners; "tarjetas" = cards; "penal" = penalty

Cross-language and synonym pairs that mean the same thing should be
treated as identical bets. A BTTS yes/no pair in either language is
still a clean binary.

# Borderline cases (subtle valid / subtle invalid)

These come up enough to warrant explicit treatment.

## Wrapper negation is always valid

"X" vs "No (X)" or "X" vs "Not X" — where X is any proposition, even a
compound one — IS a valid binary. The outer "no" negates the whole
thing, so together they cover the entire outcome space.

  VALID — "Boca gana y BTTS Sí" vs "No (Boca gana y BTTS Sí)". The
    second leg is the explicit negation of the whole conjunction.

  VALID — "River Plate gana sin recibir gol" vs "River Plate no gana
    sin recibir gol". Win-to-nil yes/no on a named team.

Distinguish this from §9: the wrapper-negation pattern has the "no"
OUTSIDE the conjunction; §9's trap has the "no" inside.

## Anytime-scorer "no" includes 0-0

"Player X scores: No" covers two cases: matches where someone other
than X scores, AND matches where no one scores. So "Player X scores
yes/no" partitions completely.

  VALID — "Cavani anota: Sí" vs "Cavani anota: No" — clean binary.

The same applies to first-scorer and last-scorer yes/no on a single
named player or team.

  VALID — "Boca anota primero" vs "Boca no anota primero (otro equipo
    o 0-0)" — the "No" side explicitly covers both the opponent-first
    and no-goal cases.

## First-goal team without the no-goal tail is invalid

"Team A scores first" vs "Team B scores first" misses the 0-0 case
where neither team scores at all.

  INVALID — "Boca anota primero" vs "River anota primero" — 0-0 is in
    neither set. trap: scope_mismatch (or use a more specific trap
    name like "missing_no_goal" if that fits).

## Clean sheet on different teams overlap

"Local clean sheet" vs "Visitante clean sheet" — both fire on 0-0
(both teams clean). Both lose on a scoreful draw or any goal-and-no-
goal match in some patterns. Not a partition.

  INVALID — "Local termina con valla invicta" vs "Visitante termina
    con valla invicta". trap: scope_mismatch.

## Single-team clean sheet yes/no is valid

  VALID — "Boca termina sin recibir gol" vs "Boca recibe al menos un
    gol" — clean binary on Boca's conceded count.

## Same proposition in two languages

If the underlying market is identical, treat as the same partition
regardless of language. The arbitrage system depends on recognizing
that "BTTS Yes" and "Ambos equipos marcan: Sí" are the same bet.

  VALID — "Both Teams To Score: Yes" vs "Ambos equipos marcan: No" —
    cross-language BTTS pair.

## Range over discrete counts: inclusive ranges partition

Inclusive integer ranges that together cover all non-negative integers
exhaustively, without overlap, form a valid partition over discrete
counts (goals, corners, cards).

  VALID — "0 a 3 goles totales" vs "4 o más goles totales" — covers
    every non-negative integer exactly once.

  INVALID — "0 a 3 goles" vs "3 o más goles" — both contain exactly
    3, overlap. trap: integer_push (or "range_overlap").

  INVALID — "0 a 2 goles" vs "4 o más goles" — exactly 3 is in
    neither. trap: scope_mismatch (or "range_gap").

## Half-with-most-goals needs the tie case

"More goals in 1H" vs "More goals in 2H" is invalid — equal-goal
matches (including 0-0) are in neither.

  INVALID — "Más goles en el primer tiempo" vs "Más goles en el
    segundo tiempo". trap: scope_mismatch.

  VALID — "Más goles en el primer tiempo" vs "Empate de goles entre
    tiempos o más en el segundo" — the tie case is absorbed into
    the second leg.

## Different markets that look similar

Two bets from different markets are almost never a partition unless
explicitly engineered to cover the same outcome space.

  INVALID — "Ambos marcan: Sí" vs "Gana Boca" — different markets;
    Boca-wins-with-BTTS overlaps both, Boca-loses-without-BTTS is in
    neither. trap: scope_mismatch.

  INVALID — "Boca anota primero" vs "Boca marca último" — first
    scorer and last scorer are different markets; one team can do
    both (single-goal match, or first and last of several) or
    neither.

# Process

For each input:

  1. Identify the bet shape on each side (1X2, double chance, O/U,
     BTTS, AH, goalscorer, combo, range, etc.).
  2. Identify the scope on each side (full match, first half, 90 min,
     tie/aggregate, named team, etc.).
  3. Check the trap taxonomy: does this pattern match any of §1–§10?
     If yes, the verdict is INVALID with that trap name.
  4. Check mutual exclusivity: enumerate the small set of outcome
     types (home win, draw, away win for 1X2; or goal counts 0,1,2,…
     for O/U; etc.). Is there any outcome on which both bets win?
     If yes, INVALID (overlap).
  5. Check exhaustiveness: is there any outcome on which neither bet
     wins (including pushes, refunds, missing draws, 0-0 on
     goalscorer markets, scope mismatches between halves/tie)? If
     yes, INVALID (gap).
  6. If you can't confidently rule out either failure, return INVALID
     with the most plausible trap name and a low- or medium-
     confidence verdict. Remember the asymmetry: the cost of a wrong
     INVALID is a missed arb; the cost of a wrong VALID is real
     financial loss.

When you have completed the analysis, call `report_partition_verdict`
with your decision. Do NOT respond with free-form text — the tool call
is the only output channel.
"""


@dataclass(frozen=True)
class LLMVerdict:
    """The LLM validator's structured output."""

    verdict: Verdict
    confidence: str
    reasoning: str
    trap_pattern: str


@dataclass(frozen=True)
class LLMValidator:
    """Wraps a Claude API client behind a typed partition-validation call.

    The client is injected so production code can pass a real
    `anthropic.AsyncAnthropic`, while tests can pass a mock. The model
    defaults to Opus 4.7 (per the project's `claude-api` skill guidance);
    override at construction time if a different tier is wanted.
    """

    client: anthropic.AsyncAnthropic
    model: str = "claude-opus-4-7"

    async def validate(
        self,
        desc_a: str,
        desc_b: str,
        context: MatchContext,
    ) -> LLMVerdict:
        """Classify a single (desc_a, desc_b, context) triple.

        Returns an `LLMVerdict` whose `verdict` is always VALID or INVALID
        (never UNKNOWN — the LLM is forced to commit by the strict tool).

        Raises:
            ValueError: If the response does not contain the expected
                tool_use block. Should be impossible with `tool_choice`
                forced to the partition tool; raised defensively so the
                caller sees a clear failure rather than a silent miss.
            anthropic.APIError: Network, auth, or rate-limit failures
                propagate from the SDK. The composition layer is
                responsible for retry policy.
        """
        bound = logger.bind(desc_a=desc_a, desc_b=desc_b, context=context)

        # Typed wrappers around dict literals so mypy can narrow the
        # Anthropic SDK's TypedDict-union parameters. Runtime shape is
        # identical to passing the dicts directly.
        thinking: ThinkingConfigAdaptiveParam = {"type": "adaptive"}
        output_config: OutputConfigParam = {"effort": "high"}
        system: list[TextBlockParam] = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        tool_choice: ToolChoiceToolParam = {"type": "tool", "name": TOOL_NAME}
        messages: list[MessageParam] = [
            {"role": "user", "content": self._build_user_message(desc_a, desc_b, context)}
        ]

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            thinking=thinking,
            output_config=output_config,
            system=system,
            tools=[PARTITION_TOOL],
            tool_choice=tool_choice,
            messages=messages,
        )

        verdict = self._parse_response(response)
        bound.info(
            "llm_partition_verdict",
            verdict=verdict.verdict.value,
            confidence=verdict.confidence,
            trap_pattern=verdict.trap_pattern,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0),
            cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return verdict

    @staticmethod
    def _build_user_message(desc_a: str, desc_b: str, ctx: MatchContext) -> str:
        return (
            f"Bet A: {desc_a}\n"
            f"Bet B: {desc_b}\n"
            "\n"
            "Match context:\n"
            f"  Competition: {ctx.competition}\n"
            f"  Stage:       {ctx.stage}\n"
            f"  Knockout:    {ctx.is_knockout}\n"
            "\n"
            f"Call the {TOOL_NAME} tool with your verdict."
        )

    @staticmethod
    def _parse_response(response: anthropic.types.Message) -> LLMVerdict:
        for block in response.content:
            if block.type == "tool_use" and block.name == TOOL_NAME:
                # The strict-tool schema guarantees this shape; cast to drop
                # the SDK's broad `object` type on tool_use.input.
                data = cast(dict[str, str], block.input)
                return LLMVerdict(
                    verdict=Verdict(data["verdict"]),
                    confidence=data["confidence"],
                    reasoning=data["reasoning"],
                    trap_pattern=data["trap_pattern"],
                )
        raise ValueError(
            f"No {TOOL_NAME!r} tool_use block in response; stop_reason={response.stop_reason!r}"
        )
