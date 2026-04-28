# AD: alfred-doctor epic — builder-grounded plan

**Linear:** [SAL-3280](https://linear.app/saluca/issue/SAL-3280/alfred-doctor-continuous-investigative-agent-for-autonomous-build)

---

# alfred-doctor: continuous investigative agent for autonomous-build pipeline

**Parent epic.** Spawns 8 atomic AD-x children, each a single component / single PR.

## Background

The autonomous-build pipeline (Wave 0..N orchestrator + Mission Control daemon + Linear + dashboard) ships work autonomously, but it has no continuous investigative agent watching its own behavior. Mining-sub findings from 2026-04-28 surfaced 12 recurring patterns (silent stalls, tool-output overrides, decomp child label drift, daemon-orphan claims, hawkman syntactic-pass-only QA, cost spike windows, mesh restart orphans, registry contradictions, etc.) that need a steady watchdog rather than ad-hoc human surveillance.

`alfred-doctor` is that watchdog: a Python service running as a systemd unit on the Oracle VM (`100.105.27.63`), cron-fired every 15 minutes, that ingests recent activity, applies surveillance heuristics, escalates to deep investigation when warranted, builds a hypothesis tree to root-cause, and either auto-executes safe corrective actions or asks Cristian via Slack `#batcave`.

## Architectural skeleton

```
alfred-doctor service (Python, runs as systemd unit on Oracle, cron-fired every 15 min):

  1. INGEST
     - Poll daemon journalctl (last interval), mesh API, GitHub PR/review state,
       Linear ticket state, v8-pipeline.json
     - Append to SQLite timeseries DB at /var/lib/alfred-doctor/state.db
     - Idempotent: re-running same interval is no-op

  2. SURVEILLANCE LOOP (claude-haiku-4-5, fast + cheap)
     - Read last interval's delta from SQLite
     - Check against 12 known patterns (mining sub findings 2026-04-28)
     - Score severity 0-10 per pattern
     - If any score >= 7 → trigger INVESTIGATE
     - If 4h since last deep-dig → trigger INVESTIGATE regardless

  3. INVESTIGATION LOOP (claude-opus-4-7, deep + expensive)
     - Input: full recent activity + triggered surveillance findings
     - Open-ended prompt: "What surprises you? What patterns are emerging?"
     - For each surprise → expand into HYPOTHESIS TREE

  4. HYPOTHESIS TREE
     - Each hypothesis: confidence_score + supporting_evidence + counter_evidence
     - Children: "if H is true, what would we expect to see? VERIFY."
     - Recursive depth limit: 4 levels (cost guardrail)
     - Root cause emerges when leaf hypothesis is byte-verified by data

  5. ACTION LAYER
     - Tier 1 (safe, auto-execute): memory write, Linear comment, dashboard alert,
       decomp ticket suggestion
     - Tier 2 (risky, Slack with full reasoning): registry edits, daemon
       restart, code PRs, ticket label changes
     - All actions logged to /var/log/alfred-doctor/actions.log

  6. LEARNING
     - Record each surveillance/investigation outcome in SQLite
     - Patterns that prove out → codified into runbook (state.db.runbook table)
     - Patterns that prove false → demoted (severity weight reduced)
     - Weekly self-summary posted to #batcave

  7. SLACK INTEGRATION
     - Tier 2 actions → message in #batcave with reasoning chain
     - Cristian replies "approve" or "deny" via thread → action taken
     - Threading preserved for audit trail

  8. DASHBOARD INTEGRATION
     - Extend v8-pipeline at http://100.105.27.63:8085/v8-pipeline
     - Add /v8-doctor/<finding-id> route showing full hypothesis tree
     - Top of v8-pipeline shows current alfred-doctor verdict + open hypotheses

Cost guardrail: $20/day hard cap; pause + Slack alert if exceeded.
```

## Phasing (waves)

| Wave | Children | Goal |
| -- | -- | -- |
| wave-1 | AD-a | Foundational ingest + SQLite schema (no LLM yet) |
| wave-2 | AD-b, AD-c, AD-e | Surveillance (haiku) + Slack helper + tier-1/2 action layer |
| wave-3 | AD-d, AD-f, AD-h | Investigation (opus) + hypothesis tree + dashboard route |
| wave-4 | AD-g | Learning subsystem + weekly self-summary |

## Children

| ID | Linear | Title | Wave |
| -- | -- | -- | -- |
| AD-a | [SAL-3281](https://linear.app/saluca/issue/SAL-3281) | Ingest service + SQLite timeseries schema | wave-1 |
| AD-b | [SAL-3282](https://linear.app/saluca/issue/SAL-3282) | Surveillance loop with 12 known patterns (claude-haiku) | wave-2 |
| AD-c | [SAL-3283](https://linear.app/saluca/issue/SAL-3283) | Slack integration for Tier-2 alerts | wave-2 |
| AD-d | [SAL-3284](https://linear.app/saluca/issue/SAL-3284) | Investigation loop (claude-opus open-ended prompt) | wave-3 |
| AD-e | [SAL-3285](https://linear.app/saluca/issue/SAL-3285) | Action layer (Tier-1 auto, Tier-2 Slack) | wave-2 |
| AD-f | [SAL-3286](https://linear.app/saluca/issue/SAL-3286) | Hypothesis tree + recursion (depth-4 cap) | wave-3 |
| AD-g | [SAL-3287](https://linear.app/saluca/issue/SAL-3287) | Learning subsystem (runbook table + weekly summary) | wave-4 |
| AD-h | [SAL-3288](https://linear.app/saluca/issue/SAL-3288) | Dashboard integration (v8-doctor route) | wave-3 |

Linear identifiers backfilled into this description after children creation. See `plans/v1-ga/AD.md` in salucallc/alfred-coo-svc for the canonical builder-grounded spec.

## Cost guardrail

Hard cap: **$20/day** across all alfred-doctor LLM calls. On exceedance, the service must pause itself and post a Slack alert in `#batcave`. Token accounting is recorded in `state.db.token_usage` table (created in AD-a) and read by every loop before issuing new LLM calls.

## References

* Mining sub findings 2026-04-28 (12-pattern roster)
* `project_mc_v1_ga_first_autonomous_merge_2026_04_26` memory
* `feedback_apev_must_be_behavioral` memory (informs surveillance pattern #5)
* `feedback_decomp_children_need_labels` memory (informs surveillance pattern #6)
* v8-pipeline dashboard at `http://100.105.27.63:8085/v8-pipeline`

## Constraints

* Python 3.11+, single repo lives at `salucallc/alfred-coo-svc/src/alfred_doctor/`
* All children are size-S, single component, single PR
* All children inherit parent labels minus `human-assigned`
* Hawkman gate-1 byte-compares APE/V section between Linear ticket and `plans/v1-ga/AD-x.md`