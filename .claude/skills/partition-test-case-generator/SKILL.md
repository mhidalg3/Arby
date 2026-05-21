---
name: partition-test-case-generator
description: Generates labeled partition validation test cases for the arby test set. Use when adding new bet types, adding new sports edge cases, or expanding tests/fixtures/partitions.yaml. Triggers on "add partition cases", "generate test cases for X market", or "expand the partition test set".
---

# Partition test case generator

Generate labeled partition validation cases for tests/fixtures/partitions.yaml.

## Format

Each case follows this YAML shape:
```yaml
- id: <unique_slug>
  desc_a: "<bet A description>"
  desc_b: "<bet B description>"
  match_context:
    competition: "<league>"
    stage: "<group|knockout|round_of_16|...>"
    is_knockout: <bool>
  expected: valid | invalid
  reasoning: "<one-sentence explanation>"
  edge_case_category: "<draw|push|own_goal|extra_time|cancellation|...>"
```

## When generating cases

1. Include both valid and invalid examples in balanced proportion.
2. Always include the win/loss trap (invalid in non-knockout, valid in knockout).
3. Cover over/under markets with both safe (X.5) and push-able (integer) thresholds.
4. For each new market type, generate at least one valid case and at least one invalid case.
5. Use realistic Argentine team names: Boca Juniors, River Plate, Racing Club, Independiente, San Lorenzo, etc.

## What NOT to do

- Don't generate cases that depend on platform-specific terminology.
- Don't generate cases for non-soccer sports.
- Don't duplicate existing cases — check the file first.