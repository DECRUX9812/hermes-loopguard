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

## Install

LoopGuard is a local package, not on PyPI. Install it from a checkout into the **same Python
environment the `hermes` command runs from** — plugins load inside Hermes' own PM-managed
environment, so an install into some other venv is invisible to Hermes:

```bash
# Find the interpreter behind your `hermes` command:
head -1 "$(command -v hermes)"    # e.g. #!/path/to/hermes-venv/bin/python

# Install from a local checkout into that environment:
/path/to/hermes-venv/bin/pip install -e /path/to/hermes-loopguard
# or: uv pip install --python /path/to/hermes-venv/bin/python -e /path/to/hermes-loopguard
```

The `-e` (editable) install matters: stock Hermes' PM rewires `sys.path` and drops every
`site-packages` entry that is not its own, so a regular (non-editable) install is invisible to the
in-process plugin loader. An editable install survives because its `.pth` adds the source directory
instead. (The cron-script path works either way: the watcher shim embeds the install's site dirs.)

LoopGuard has no runtime dependencies beyond what stock Hermes ships (pyyaml is optional; a
built-in fallback covers YAML), so it loads cleanly inside the PM environment.

## Quickstart

```bash
# 1. Enable the plugin (opt-in; writes plugins.enabled in $HERMES_HOME/config.yaml)
hermes plugins enable loopguard

# 2. Optional: route gate questions through a cheap model instead of local
#    heuristics — any OpenAI-compatible provider. Add to $HERMES_HOME/config.yaml
#    (the API key goes in $HERMES_HOME/.env, never in config.yaml):
```

```yaml
providers:
  my-provider:
    api: http://127.0.0.1:8317/v1   # any OpenAI-compatible endpoint
    name: OpenAI-compatible
    key_env: MY_PROVIDER_API_KEY
    transport: chat_completions
auxiliary:
  loopguard_gate:
    provider: my-provider
    model: a-cheap-fast-model
```

```bash
# 3. Install a watcher (creates $HERMES_HOME/scripts/loopguard_<name>.py
#    and a cron job named lg-<name>)
hermes loopguard watch install examples/issue_triage.yaml --schedule "every 15m"

# 4. Trigger one run, then check what the gate did
hermes cron run lg-issue-triage
hermes loopguard stats

# 5. Optional: dashboard page in the Hermes web UI
hermes loopguard dashboard install
```

A full scripted walkthrough against a real Hermes cron job lives in
[examples/demo/](examples/demo/) (`run_demo.sh`, with a recorded `sample-output.txt`).

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
on_error: wake  # watcher error policy: wake (fail open) | skip
typesafe:
  api_key_env: TYPESAFE_API_KEY
  timeout: 30
pricing:
  est_tokens_per_run: 25000
  usd_per_mtok: 3.0
```

`on_error` decides what a watcher run does when it fails (a broken decision
store, a source that crashes, a backend error): `wake` (the default) fails
open and wakes the agent so LoopGuard is never worse than not having it;
`skip` keeps the agent asleep (`{"wakeAgent": false}`). Either way the error
is logged and a `LoopGuard: ...` warning goes to stderr.

## Settings

All LoopGuard settings live in one place: **Settings > Plugins > loopguard** in the Hermes
Desktop app (the gear on the plugin's row in **Capabilities > Plugins** opens it). Hermes builds
the form from the plugin's `config_schema` and stores values under
`plugins.entries.loopguard.settings` in `config.yaml`. LoopGuard builds no settings UI of its own;
its dashboard page is a work surface only.

| Setting | Type | Default | What it does |
|---|---|---|---|
| Shadow mode | switch | off | Log proposed gate decisions without changing behavior |
| Semantic backend | dropdown | `auto` | `auto`, `typesafe`, `openai`, `hermes` or `local` |
| Route confidence threshold | number | `0.6` | Minimum Choice probability for a route to admit; below it the item falls back to review |
| Per-item spend cap (tokens) | number | `400000` | Fallback per-item token cap (a watcher spec's own `spend_cap_tokens` wins) |
| Tool gate | switch | off | Classify shell commands in automations and block or ask before risky ones |
| Tool gate semantic classifier | switch | off | Second opinion from the semantic backend on unclear commands |
| Completion continuations cap | number | `3` | How many times `pre_verify` may nudge on an unmet GOAL.md |
| Computer-use gates | switch | off | Enable `jev_computer` and the verifier / stall / safety gates |
| Computer-use shadow mode | switch | off | Log computer-use verdicts without changing behavior |
| TypeSafe model (pinned) | text | `jev-1.14.0` | Explicit versioned id; aliases move between releases |
| Computer-use vision model chain | JSON | `["jev-1.14.0", "jev-1.13.0", "openai:gemini-3.8-flash-high"]` | Ordered fallback chain for grounding and verification |
| TypeSafe API key | masked secret | — | Stored in `.env` through Hermes, never in `config.yaml`; read via `agent.secret_scope` |

Values saved in Settings > Plugins are **layered over** `<HERMES_HOME>/loopguard/config.yaml`,
and only keys you actually save there — so existing config files keep working, and keys that are
not in the form (per-route thresholds, the OpenAI-compatible endpoint, pricing estimates, driver
details) stay in that file. The cron shim reads the same layered config.

## What it touches

For the catalog disclosure (rule 13) — everything LoopGuard does that a user should know before
installing:

- **Network calls:** TypeSafe (`api.typesafe.ai`, gate questions and vision checks; only when a
  key is set), any OpenAI-compatible endpoint you configure (`openai.base_url`, e.g. a local
  proxy), and the Hermes aux model (`auxiliary.loopguard_gate`). **Telemetry: none** — LoopGuard
  reports nothing anywhere.
- **Files:** its SQLite decision log and data under `<HERMES_HOME>/plugin-data/loopguard/`, its
  config under `<HERMES_HOME>/loopguard/`, the cron watcher scripts it installs into
  `<HERMES_HOME>/scripts/` when you run `hermes loopguard watch install`, and a read of the
  `GOAL.md` in the working directory when the completion check is active.
- **Shell commands:** the `kanban` route runs `hermes kanban create`; the computer-use driver
  (off by default) spawns the `cua-driver` CLI you point it at.
- **Background processes:** none.
- **Stored credentials:** the TypeSafe key (`TYPESAFE_API_KEY`) and, if you configure them, any
  `api_key_env` for the OpenAI-compatible backend — all read only through Hermes's secret scope,
  never from `.env` or any other credential file directly.

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

## Computer use

`jev_computer` is LoopGuard's cheap computer-use driver for stock Hermes: instead of paying a big
model a full vision turn for every screenshot decision, it runs the cheapest check that can answer
the question and escalates only when it must:

1. **Plan** — one cheap text call (Gemini Flash-Lite, no screenshot) turns the task into steps.
2. **Ground** — the accessibility tree first (free), then a Jev 1.14 **Box** locate: one request
   carries a Box question plus a presence Noul on a downscaled screenshot (1568 px max side), so a
   "not found" never clicks a hallucinated coordinate. Low confidence gets one zoomed retry.
3. **Verify** — after every mutating action, one Jev Noul: "Does `after` show *the Name field
   contains 'Ada Lovelace'*?" The free path (driver `effect:"confirmed"`) skips it when the driver
   itself checked over a11y.
4. **Stall detection** — pure-code dHash of consecutive frames catches no-ops and A-B-A-B loops
   before any model call.
5. **Safety gate** — before mutating actions, a screen-level Choice (payment / deletion /
   send_or_publish / account / none) plus a confirm-dialog Noul; a risky category in the ambiguous
   band gets ONE action-level second opinion. Bias is to pause, and payment/deletion/account
   changes are never pre-authorized by the instruction.
6. **Recover** — retry once, replan once, escalate that single step to a strong vision model (max
   2 per task), then stop. Terminal states are explicit.

Enable it in `config.yaml` (off by default; `shadow: true` logs verdicts without changing
behavior — start there):

```yaml
plugins:
  enabled: [loopguard]
computer_use:
  enabled: true
  shadow: false
```

The plugin registers the **`jev_computer`** tool (plan → ground → act → verify in one call, the
main token saver) and **`loopguard_cu_status`**, and hooks Hermes's own `computer_use` tool:
`pre_tool_call` runs the safety + stall gates, `transform_tool_result` attaches
`VERIFIED` / `NOT VERIFIED: <reason>` to the result so the model doesn't re-screenshot to check its
own click.

To try it hands-on: `examples/cu_bench/stack.sh start` boots an isolated Xvfb :97 stack
(firefox/chrome, thunar, GTK settings, AT-SPI bus), `examples/cu_demo/` has the demo tasks, and the
dashboard panel (screenshot: `examples/demo/dashboard-cu.png`) shows the live step log with
thumbnails, boxes and running cost.

### Jaggedness (measured, not marketed)

Numbers from `hermes loopguard eval-cu` over 45 labeled cases at 1280x800 — 27 locate (22 present,
5 absent), 8 verify pairs, 10 safety screens, every case run 3x. TypeSafe itself labels images and
Box "Dreamer-ready", not production, and the eval shows why:

Locate (click point inside the true rect; 22 present + 5 absent targets x 3 repeats):

| arm | hit rate | mean IoU | absent acc | latency | $ / locate | hit flips |
|---|---|---|---|---|---|---|
| Jev 1.14 Box + presence | 88% | 0.59 | 100% | 0.32 s | $0.0004 | 2 cases |
| Gemini 3.8 Flash (JSON coords) | 88% | 0.79 | 100% | 8.0 s | $0.0041 | 5 cases |

Same hit rate, twelve times the latency and ten times the price for the big-model fallback.
Calibration: both arms put virtually every answer in the 0.75-1.00 confidence bucket (57 of 62
Jev present-target answers, 66 of 66 Gemini) — there is almost no useful confidence spread, and
in that bucket Jev hits 93% / Gemini 88%. Threshold sweep: Gemini is flat at 88% across the whole
presence x box-confidence grid; Jev swings 77-88%, and the shipped defaults (0.6 / 0.5) sit at
the sweep optimum — no retuning needed.

Verify (before/after pairs, 8 x 3): accuracy / precision / recall 100% on both arms, 0 false
passes, 0 false fails, 0 flips across repeats, flat on the verify-threshold sweep (0.3-0.9). A
clean sheet here — the verifier risk shows up in the bench's judge loops (see [BENCH.md](BENCH.md)),
not on these pairs.

Safety (10 screens x 3, empty authorized set = unattended worst case):

| arm | policy | accuracy | misses (should pause, didn't) | false pauses |
|---|---|---|---|---|
| Jev | stage 1 alone | 60% | **0** | 12 of 30 |
| Jev | two-stage | 70% | **0** | 9 of 30 |
| Gemini | stage 1 alone | 67% | **0** | 10 of 30 |
| Gemini | two-stage | 67% | **0** | 10 of 30 |

The dangerous column stays at zero everywhere; the cost is nuisance pauses. Jev's two-stage action
check clears the plain-settings false pause (3 of its 12); Gemini's clears none. False pauses
persist across the safety-threshold sweep (0.1-0.6; misses stay 0 everywhere).

The AT-SPI arm is not scored: the bench stack was retired before a clean live pass. What we
measured of it: when the a11y tree is available, label lookups are exact (pixel-perfect against
the ground-truth rects); the jaggedness is *availability* — under a loaded desktop, single-shot
walks returned empty trees 15/15 times at canonical-run time, which is why the eval now retries
walks the way `find_element` does.

What jaggedness we actually hit (all reproducible; failure thumbnails ship in
`examples/cu_eval/results/final*/thumbs/`):

- **Small targets.** The 13x13 px newsletter checkbox is a Jev flip case: miss, hit, hit on
  identical input (`loc-widgets-newsletter-jev-r0.jpg`); Gemini clears it 3/3.
- **Dense toolbars.** thunar's 37x35 toolbar buttons: Jev misses the "Search for Files..." toggle
  0/3; Gemini wobbles on "Open Parent" (2/3, `nat-thunar-parent-gemini-r2.jpg`).
- **Look-alike buttons.** The identical "Save" / "Don't save" dialog buttons: Jev picks the wrong
  one or straddles both, 0/3 (`loc-dup-save-jev-r0.jpg`); Gemini is clean there.
- **Ambiguous targets.** With two text editors / windows on screen, "the File menu" or "the text
  area" is under-specified — plain form fields that are trivial alone become flip cases for Gemini
  (`loc-contact-name-gemini-r1.jpg`: 1/3), and even a full-width textarea flips for Jev
  (`nat-mousepad-textarea-jev-r0.jpg`: 2/3).
- **Dark themes.** A dark copy of the contact form flips Gemini's Name field (1/3); Jev is
  unaffected.
- **Variance across repeats** on identical inputs: 2 flip cases for Jev, 5 for Gemini, on
  byte-identical inputs. Repeated identical calls do not always agree.
- **Safety false pauses** (bias-to-ask costs human attention): Jev reads the shop's
  "Buy now — $49/mo" as a payment risk and a plain signup form as an account change; a plain
  contact form reads as "send". The two-stage action check clears only some of these; the misses
  column stays at zero.

## Computer-use bench (Phase 2b)

`examples/cu_bench/` holds the Xvfb benchmark that drives the computer with the cheap typed path
(Jev + AT-SPI) and compares it against a big-model computer-use loop. Results and the verdict:
[BENCH.md](BENCH.md).

Run it with the **lg-venv interpreter** (it has `typesafe-sdk`, PyYAML and Pillow — the system
python3 does not, and every Jev call would silently fall back to the CLIProxy model) and
`PYTHONPATH=src`:

```bash
# stack + runs both hold the shared locks; never skip flock
~/Code/ship-test/lg-venv/bin/python -c "import typesafe_sdk"   # interpreter check
flock /tmp/lg-cu/stack.lock examples/cu_bench/stack.sh start    # Xvfb :97 stack (if down)

cd examples/cu_bench
flock /tmp/lg-cu/stack.lock env PYTHONPATH=src \
  ~/Code/ship-test/lg-venv/bin/python run_bench.py --arm jev      --tasks all --repeat 1
flock /tmp/lg-cu/stack.lock env PYTHONPATH=src \
  ~/Code/ship-test/lg-venv/bin/python run_bench.py --arm baseline --tasks all --repeat 1
env PYTHONPATH=src ~/Code/ship-test/lg-venv/bin/python run_bench.py --summary
```

`--arm jev` preflights the interpreter, the `TYPESAFE_API_KEY` and one tiny real `jev-1.14.0`
call, and exits non-zero before any spend when they are missing; `--summary` flags any result
row whose vision calls were served by a non-Jev model.

## Development

Run unit tests:

```bash
PYTHONPATH=src pytest -q
```

End-to-end tests (`tests/test_e2e_hermes.py`) run the real stock `hermes`
binary in subprocesses against a throwaway `HERMES_HOME`. The hermes path is
machine-specific, so it is required via `LOOPGUARD_E2E_HERMES` (the module
skips cleanly when unset). `LOOPGUARD_E2E_INSTALLS` optionally points at an
already-bootstrapped PM `installs/` directory so the first hermes command in
the test home skips the slow (~240 MB) bootstrap:

```bash
LOOPGUARD_E2E_HERMES=/path/to/hermes-venv/bin/hermes \
LOOPGUARD_E2E_INSTALLS=/path/to/prepared/installs \
PYTHONPATH=src pytest -q tests/test_e2e_hermes.py
```

License: MIT.
