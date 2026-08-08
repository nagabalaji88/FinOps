# Guardrails

Two layers protect every run: the engine's own deterministic rules, and
[NVIDIA NeMo Guardrails](https://github.com/NVIDIA/NeMo-Guardrails) rail configurations for
the production agents whose mistakes carry regulatory consequences — Customer Service, AML
Investigation, Credit Risk and Collections.

```
request ──▶ input_rails ──▶ planner ─▶ retriever ─▶ memory ─▶ LLM ⇄ tools
                 │                                              │
            NeMo input rails                                    ▼
            (block before any                            validation
             system of record                                   │
             is touched)                                        ▼
                                                          guardrails ──▶ approval ──▶ response
                                                               │
                                                    engine rules + NeMo output rails
```

## What each agent's rails enforce

Rail configurations live in `backend/app/guardrails/configs/<agent_key>/` as ordinary NeMo
projects — `config.yml`, Colang flows in `rails.co`, and `prompts.yml` — so they can be
reviewed, diffed and versioned like any other policy artefact.

### Customer Service

| Side | Rail | Blocks |
|---|---|---|
| input | `banking prompt integrity` | Prompt injection, and attempts to talk past a control: *skip the authentication*, *don't log this*, *pretend you are an approver* |
| input | `customer service scope` | Requests for investment advice the agent is not licensed to give |
| input | `self check input` | Model-judged: another customer's data, credential harvesting, instruction override |
| output | `sensitive disclosure` | Masks unmasked PAN, Aadhaar, card and account numbers, leaving the last four |
| output | `self check output` | Model-judged: third-party data, advice, unsupported figures or promises |

### AML Investigation

| Side | Rail | Blocks |
|---|---|---|
| input | `banking prompt integrity` | As above |
| input | `financial crime scope` | Requests to evade monitoring, reporting thresholds or sanctions screening; requests to structure, layer or conceal funds; requests for text that would tip a subject off |
| input | `self check input` | Model-judged equivalents, while allowing genuine investigative questions |
| output | `tipping off` | Any response telling a subject they are monitored, investigated or reported |
| output | `sensitive disclosure` | As above |
| output | `self check output` | Model-judged: evasion guidance, unapproved filing claims, unsupported conclusions |

**Tipping off** is a criminal offence in most jurisdictions — PMLA s.63 in India, POCA
s.333A in the UK — which is why it blocks outright rather than being redacted.

### Credit Risk

| Side | Rail | Blocks |
|---|---|---|
| input | `banking prompt integrity` | Injection and control bypass |
| input | `fair lending` | Any request to weigh age beyond the policy limits, sex, marital status, pregnancy, religion, caste, race, ethnicity, disability, nationality or the applicant's neighbourhood — and redlining language |
| input | `self check input` | Model-judged: approving over a knockout, hiding a decline reason, recording a decision without running the models |
| output | `fair lending output` | The same characteristics appearing as a *reason* in the recommendation |
| output | `sensitive disclosure` | Identifier masking |
| output | `self check output` | Model-judged: a decline without actionable reason codes, a figure no tool produced, a recommendation presented as a decision |

The prohibited characteristics are those named in the Equal Credit Opportunity Act
s.701(a), the RBI Fair Practices Code and Article 15 of the Indian Constitution.

### Collections

| Side | Rail | Blocks |
|---|---|---|
| input | `banking prompt integrity` | Injection and control bypass |
| input | `collections control bypass` | Any request to contact someone despite a cease instruction, withdrawn consent, open dispute or hardship arrangement, or outside permitted hours |
| input | `collections conduct` | Threats of arrest, criminal proceedings or public disclosure; contacting an employer, relative, neighbour or other third party about the debt |
| input | `self check input` | Model-judged equivalents, including pressing for more than the assessed surplus |
| output | `collections conduct output` | The same conduct appearing in the answer |
| output | `sensitive disclosure` | Identifier masking |
| output | `self check output` | Model-judged: a suppressed action, an unaffordable instalment, a coercive tone, a figure no tool produced |

These are the RBI Fair Practices Code rules for lenders' recovery agents, and their FDCPA
s.806–807 equivalents.

Agents with no configuration under `configs/` are unaffected: the rail node reports itself
skipped and the run proceeds.

## Two rules the integration keeps

**Rails never fail open.** Three things could silently disable a control, and each of them
blocks instead:

| Situation | Behaviour |
|---|---|
| A rail action raises | Blocked. NeMo's dispatcher logs an action exception and returns `None`, which Colang reads as "nothing found" — so every detector is wrapped at *registration* rather than at definition, and a crash becomes a `rail_error` block |
| The rail config will not load, or `nemoguardrails` is not installed | Blocked, with `rails_unavailable` naming the reason |
| The LLM-backed rail cannot reach a model | Blocked |

The only way to run these agents without rails is to say so: `NEMO_GUARDRAILS_ENABLED=false`,
which is a recorded configuration decision and is reported in Connected Services.

**Deterministic first.** The pattern rails need no credentials, so they hold in every
environment, and they are ordered ahead of the model-backed rails so an obviously bad
prompt is refused without spending a model call. When no provider is configured — or
`NEMO_LLM_RAILS_ENABLED=false` — the self-check flows are removed from the configuration
rather than left to error on every request, and the status says
`deterministic rails only`.

## Where the rail calls go

The LLM-backed rails do not get their own credentials or HTTP client. `RouterLLM`
implements NeMo's framework-agnostic `LLMModel` protocol on top of the platform's model
router, so a rail call is routed, retried, circuit-broken and **cost-metered** exactly like
an agent's own model call, and lands in the same cost ledger tagged
`purpose: guardrail`.

## What you see when a rail fires

An input rail refusal ends the run at the first node:

```json
{
  "status": "failed",
  "error_type": "GuardrailViolation",
  "node_path": ["input_rails"],
  "tool_call_count": 0,
  "llm_call_count": 0
}
```

Nothing read a system of record. In the Execute console the event feed shows
*Input rails blocked the run* with the rule that fired; the finding is recorded on the
execution, in the span, in the event log and in the audit trail:

```json
{"rule": "control_bypass", "severity": "high", "action": "blocked",
 "detail": "\\b(skip|bypass|…)\\b", "source": "nemo"}
```

Output rails behave differently by design: `sensitive disclosure` **masks** and lets the
answer through, `tipping off` **blocks**.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `NEMO_GUARDRAILS_ENABLED` | `true` | Master switch. False runs the engine's own rules only |
| `NEMO_LLM_RAILS_ENABLED` | `true` | The self-check rails. Also off automatically with no provider |
| `GUARDRAILS_MODEL` | *(empty)* | Model for rail calls. Empty lets the router choose |
| `NEMO_BLOCK_ON_INPUT_RAIL` | `true` | Only turn off while tuning a new rail |

Install the dependency with the `guardrails` extra; the published image already has it:

```bash
pip install -e ".[guardrails]"
```

## Adding rails to another agent

1. Create `backend/app/guardrails/configs/<agent_key>/` with `config.yml`, `rails.co` and
   `prompts.yml`. Copy `aml_investigation` — it exercises both a blocking and a masking
   output rail.
2. Add any new pattern to `app/guardrails/patterns.py` so the engine's own rules and the
   rails cannot drift apart.
3. Add the detector to `DETECTORS` in `app/guardrails/actions.py`. It is wrapped for
   fail-closed behaviour automatically; only add to `TRANSFORMS` if it rewrites the
   message rather than judging it.
4. Add cases to `backend/tests/test_guardrails.py` — both the behaviour that must be
   blocked *and* the agent's legitimate work that must not be.

The rail node picks the configuration up on the next run; nothing else needs changing.

## Testing

`backend/tests/test_guardrails.py` (42 tests) is written the way a control is tested: every
rail is proven to fire on the behaviour it exists to stop, proven not to fire on the
legitimate work of the same agent, and proven to fail closed when it cannot run — including
a test that registers a deliberately crashing detector and asserts the run is refused.

```bash
cd backend && .venv/bin/pytest tests/test_guardrails.py -q
```
