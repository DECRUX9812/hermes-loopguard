# hermes-loopguard

Stop automations from burning frontier model tokens on unchanged state, routine triage, and retry loops.

Cron and kanban automations pay frontier models to inspect empty queues,
reread unchanged issues, and evaluate whether failed tasks should retry.
LoopGuard puts those decisions on a cheap, typed path before the agent wakes.

## How it works

```
cron tick -> loopguard_<watcher>.py shim
               |
               v
      source (file | command | python) -> items[]
               |
               v
     CHANGE GATE (pure code, 0 tokens)
       Unchanged / terminal / claimed-by-live-run -> skip
       No admitted items -> {"wakeAgent": false} (Hermes never calls a model)
               |
               v
     SEMANTIC GATE (cheap, typed classifier)
       Questions: Choice, Noul, Score (one call per item)
       Threshold check: below threshold -> review queue
               |
               v
     ROUTER (route -> handler)
       agent   : claim item, build bounded brief, wake agent with {"wakeAgent": true}
       kanban  : hermes kanban create (idempotent admission)
       review  : enqueue in SQLite (hermes loopguard review)
       drop    : suppress item, log decision
```

When every item is unchanged or filtered out, the runner emits `{"wakeAgent": false}`.
Hermes terminates the job immediately without building a prompt or dispatching an LLM turn.

## Quickstart

Install into the same Python environment as Hermes:

```bash
# From git or a local clone (the package is not on PyPI)
pip install "hermes-loopguard[typesafe] @ git+https://github.com/your-org/hermes-loopguard.git"
# Or in editable mode from a local checkout:
pip install -e /path/to/hermes-loopguard

# Enable plugin in Hermes
hermes plugins enable loopguard

# Install watcher, schedule cron, and verify
hermes loopguard watch install examples/issue_triage.yaml --schedule "every 15m"
hermes cron run lg-issue-triage
hermes loopguard stats

# Optional: install dashboard plugin
hermes loopguard dashboard install
```

## Backends

LoopGuard evaluates gate questions using a fast, cheap backend. `backend: auto` evaluates
backends in order: TypeSafe, OpenAI-compatible, Hermes auxiliary task, then local heuristics.

| Backend | Requirements | Features |
|---|---|---|
| `typesafe` | `TYPESAFE_API_KEY`, `typesafe-sdk` | Calibrated probabilities, fast Jev model |
| `openai` | `base_url`, `model`, `api_key_env` | JSON or logprobs option probabilities |
| `hermes` | `auxiliary.loopguard_gate` in Hermes config.yaml | Routes through any cheap Hermes provider |
| `local` | None | Pure Python keyword and regex heuristics |

Global configuration in `<HERMES_HOME>/loopguard/config.yaml`:

```yaml
backend: auto
shadow: false
typesafe:
  api_key_env: TYPESAFE_API_KEY
  timeout: 30
pricing:
  est_tokens_per_run: 25000
  usd_per_mtok: 3.0
```

## Watcher spec reference

Watchers are defined in YAML under `<HERMES_HOME>/loopguard/watchers/<name>.yaml`:

```yaml
name: issue-triage
source:
  command: ["gh", "issue", "list", "--state", "open", "--json", "number,title,body,labels"]
id_field: number
fingerprint_fields: [title, body, labels]
gate:
  route_question: kind
  questions:
    kind:
      type: choice
      instructions: What does this issue need?
      criteria:
        bug: defect in existing behaviour
        feature: request for new behaviour
        duplicate_or_spam: spam or duplicate
    actionable:
      type: noul
      instructions: Is there enough detail to start work?
  routes:
    bug:
      handler: agent
      min_prob: 0.6
      when: {actionable: ">=0.6"}
      brief: "Reproduce first. Propose a minimal fix."
    feature:
      handler: review
      min_prob: 0.5
    duplicate_or_spam:
      handler: drop
      min_prob: 0.75
  default_route: review
```

Questions support `choice` (categorical probabilities), `noul` (scalar probability of true),
and `score` (ordered numeric levels). Items below probability thresholds route to `default_route`.

## Routing, retries, and terminal states

Items transition across states: `new`, `claimed`, `completed`, `blocked`, `budget_exhausted`, `dropped`, `review`, and `failed`.
Items only retry when evidence changes (new content fingerprint) up to `max_attempts`.

For Kanban boards, LoopGuard performs admission before task creation, mapping items to board tasks
with an idempotency key and `--max-retries`. On stock Hermes, LoopGuard cannot veto dispatch at zero
tokens because Hermes lacks a pre-dispatch hook; worker-side spend caps in `pre_tool_call` halt
runaway workers after one short turn by blocking non-kanban tools.

## Tool gate

An optional policy gate in `config.yaml` (`tool_gate.enabled: true`) inspects tool calls via
Hermes's `pre_tool_call` hook. It classifies calls into read, write, network, shell, or destructive
actions. The tool gate can block destructive calls or request human approval, but never weakens
Hermes's built-in confirmation prompts. Scope is constrained to automations.

## Completion check

LoopGuard provides completion verification for goal-driven workflows:

```bash
hermes loopguard check GOAL.md --cwd /path/to/repo
```

When active, `pre_verify` checks criteria in `GOAL.md` and nudges the model to continue if criteria
remain unmet, capped by `completion.max_continuations` (default 3).

## Shadow mode and eval harness

In shadow mode, LoopGuard logs proposed classifications without suppressing runs:

```bash
hermes loopguard shadow on --watcher issue-triage
```

Run offline evaluations against labeled test cases:

```bash
hermes loopguard eval examples/eval/issue_triage_cases.jsonl --watcher examples/issue_triage.yaml
```

## Savings view

Inspect run savings, avoided tokens, and gate overhead:

```
LoopGuard: 41 runs skipped (87%), ~1.0M tokens / $3.08 avoided, gate cost $0.02, $0.41 per completed task
Watcher         Polls  Skipped  Woken  Avoided   Routes (agent/review/drop)
issue-triage       47       41      6     1.0M   6 / 2 / 33
```

Install the dashboard UI plugin:

```bash
hermes loopguard dashboard install
```

The web dashboard displays live KPI cards, sparklines, stacked skipped vs woken bar charts,
per-watcher route distributions, a 25-item decision log, and an interactive review queue.

## When to use Hermes cron monitor mode

Use Hermes's built-in cron monitor mode (`monitor_script` or `monitor_url`) if a whole-output hash
of the entire command output is sufficient to detect change. Use LoopGuard when jobs process
collections of items requiring per-item tracking, semantic triage, claim leases, and review queues.

## Development

Run unit tests:

```bash
PYTHONPATH=src pytest -q
```

License: MIT.
