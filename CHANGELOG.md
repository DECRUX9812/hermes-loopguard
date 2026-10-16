# Changelog

## 0.1.0 (2026-10-16)

First public release.

- Change gate: per-item fingerprints, claim locks and terminal states. Unchanged cron and kanban polls
  return `{"wakeAgent": false}` with zero model calls.
- Typed semantic gate (Choice / Noul / Score) with TypeSafe Jev, OpenAI-compatible, Hermes auxiliary
  and local backends, per-route thresholds, a review queue and shadow mode.
- Tool gate (`pre_tool_call`) and completion check (`pre_verify` against `GOAL.md`).
- Decision log, `hermes loopguard stats`, eval harness and a dashboard work surface.
- Computer use: the `jev_computer` tool and verifier / stall / safety gates on Hermes's
  `computer_use` tool, with a bench (BENCH.md) and the `eval-cu` reliability eval.
- Settings declared in `plugin.yaml` `config_schema` (Settings > Plugins > loopguard).
