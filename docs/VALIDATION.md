# Conformance suite

Twenty inputs across five of the seven implemented agents, and an execution validation agent that
routes each one to the agent that owns it, runs it for real, acts as the human reviewer
when a run suspends for approval, and asserts the expected behaviour.

It is a release gate, not a demo. The verdict is reproducible and the exit code is
meaningful.

**One command is enough.** `validate` prepares whatever is missing — schema, platform seed
(identities, agent registry, knowledge corpus, watchlists) and the sample banking dataset —
then runs the inputs **one at a time in order**, printing each agent's result as it
arrives. Every preparation step is idempotent, so on an already-prepared deployment it
costs one query and moves on. Use `--no-setup` to skip preparation entirely.

```bash
cd backend
python -m app.cli validate                                  # all 20, prepares if needed
python -m app.cli validate --agent aml_investigation        # one agent
python -m app.cli validate --scenario KA-01 --scenario CS-04
python -m app.cli validate --tag hitl                       # governance paths only
python -m app.cli validate --output report.md --format markdown
python -m app.cli validate --fail-on-blocked                # strict CI mode
python -m app.cli validate --no-setup                       # assume prepared
python -m app.cli validate --quiet                          # verdicts only
```

Each input produces a block like this as it completes:

```
[ 1/20] CS-01   customer_service      Authenticated balance inquiry
------------------------------------------------------------------
  input   {"query": "What is the balance on my savings account right now?",
          "identifier": "CUS-100001", "pin": "1000"}
  result  PASS · 4.2s · $0.00312 · 2 tools · 12 spans · 11/11 checks
  tools   authenticate_customer, get_account_balance
  output  Your savings account ****4821 has an available balance of INR 1,204,338.20
          as of 05 August 2026. There are no holds on the account.
```

A failure names the check that broke; a blocked scenario names the reason. Runs are
sequential by default so the output reads top to bottom; `--concurrency` trades that
readability for speed.

| Exit code | Meaning |
|---|---|
| 0 | every scenario evaluated and passed |
| 1 | at least one scenario failed or errored |
| 2 | filters matched no scenarios |
| 3 | scenarios were blocked and `--fail-on-blocked` was set |

## The scenarios

Four per agent, chosen so the suite covers the happy path, a security control, a
governance gate and a negative case for each.

| ID | Agent | Scenario | What it proves |
|---|---|---|---|
| CS-01 | Customer Service | Authenticated balance inquiry | Auth then ledger read; no full account number in the answer |
| CS-02 | Customer Service | Unauthenticated request refused | No account data before authentication, even when asked directly |
| CS-03 | Customer Service | Card dues and late-payment policy | System-of-record read plus policy lookup; no full card number |
| CS-04 | Customer Service | Fraud allegation escalates | Sentiment → escalation → **human approval** → resume |
| KYC-01 | KYC & Onboarding | Sanctions hit blocks onboarding | A watchlist match is reported and never approved |
| KYC-02 | KYC & Onboarding | PEP match requires EDD | A PEP is never straight-through approved |
| KYC-03 | KYC & Onboarding | Clean applicant screens clear | No false positive on an unrelated name |
| KYC-04 | KYC & Onboarding | Risk scoring | The weighted model runs and drives a stated action |
| AML-01 | AML Investigation | Monitoring surfaces structuring | Rules run over the real ledger; typologies are named with figures |
| AML-02 | AML Investigation | Behavioural profile | The baseline is quantified, not described |
| AML-03 | AML Investigation | Case timeline | Alerts and transactions placed in sequence |
| AML-04 | AML Investigation | SAR drafting gated, **rejected** | A rejected filing is never reported as filed |
| IR-01 | Investment Research | Portfolio valuation | Values from holdings and prices; disclaimer guardrail applied |
| IR-02 | Investment Research | VaR and expected shortfall | Risk computed from price history, method stated |
| IR-03 | Investment Research | Sector comparison | Ordered ranking from the instrument master |
| IR-04 | Investment Research | Fundamental analysis | Ratios computed; risk flags surfaced |
| KA-01 | Knowledge Assistant | Incident severity | Correct answer **and** cited |
| KA-02 | Knowledge Assistant | SAR filing deadline | Regulatory figure quoted from policy, not recalled |
| KA-03 | Knowledge Assistant | Product terms | Exact figure reproduced |
| KA-04 | Knowledge Assistant | Out-of-corpus question | Refuses to answer rather than inventing |

Tags let you slice the suite: `happy-path`, `security`, `negative`, `hitl`, `screening`,
`sanctions`, `pep`, `risk-model`, `monitoring`, `typology`, `profiling`, `timeline`,
`sar`, `portfolio`, `risk`, `quantitative`, `sector`, `fundamentals`, `rag`, `citations`,
`hallucination`, `guardrail`, `pii`, `product`, `compliance`.

## What gets asserted

Model wording varies between runs; behaviour must not. So the assertions are mostly
structural, with narrow content checks where a specific fact matters.

| Check | Fails when |
|---|---|
| `status` | the run did not reach the expected terminal state |
| `tools_called` | a required tool never ran |
| `tools_forbidden` | a tool that must not run did |
| `tool_success` | an invoked tool failed |
| `response_length` | the answer is too short to be an answer |
| `must_match` | a required pattern is absent from the response |
| `must_not_match` | a forbidden pattern is present (unmasked PII, a false claim) |
| `citations_retrieved` | retrieval returned nothing when citations are required |
| `citations_referenced` | the answer cites no source when citations are required |
| `approval_raised` | a gated action ran without suspending for a human |
| `approval_on_tool` | the gate fired on the wrong tool |
| `approval_unexpected` | an approval was raised when none should have been |
| `guardrails_applied` | a required guardrail (for example the research disclaimer) did not fire |
| `engine_validation` | the engine's own validation node did not pass a named check |
| `cost_within_cap` | the run cost more than the scenario allows |
| `latency_within_sla` | the run was slower than budgeted (**warning**, not a failure) |
| `output_keys` | the structured output is missing a required key |
| `trace_recorded` | no spans were written — the observability guarantee broke |

Every check reports its own detail, so a failure names the behaviour that broke rather
than just marking the scenario red.

## How the validator drives a run

1. **Prepare** — creates the schema, seeds the platform and loads the sample banking
   dataset if any of them are missing. Idempotent, and skippable with `--no-setup`.
2. **Preflight** — confirms the agents are registered and active, a provider is
   configured, the sample dataset is present for scenarios that need it, and the reviewer
   identity exists. Warnings are printed before anything executes.
3. **Submit** — routes the scenario input to its agent through the same engine path the
   API uses, as the `operator@finops.local` identity, with trigger `validation`.
4. **Review** — when a run suspends on an approval gate, the validator finds the pending
   request, records the scenario's decision as `approver@finops.local` with a comment
   naming the scenario, and resumes the execution from its checkpoint. Both approval and
   rejection paths are exercised by the suite.
5. **Observe** — reads back the execution, its spans (tool invocations come from the
   trace, so failed calls still count as invoked), its approvals and its structured output.
6. **Assert** — evaluates every expectation, records a verdict of pass, fail, blocked or
   error, prints the result block, and moves to the next input.

## Verdicts

- **passed** — every critical check passed.
- **failed** — at least one critical check failed. This is an agent defect.
- **blocked** — the run could not be evaluated for an environmental reason, almost always
  a missing or failing LLM provider (`ProviderNotConfiguredError`, `ProviderError`,
  `CircuitOpenError`). Reported separately because it is not something an agent author
  can fix, and it must not masquerade as a pass.
- **error** — the validator itself could not complete: the agent is not registered, is
  paused, or the run never reached a terminal state.

By default `blocked` does not fail the build, because a missing key in a preview
environment should not look like a broken agent. Use `--fail-on-blocked` where the suite
must be conclusive.

## API

The suite is also reachable from the console and from pipelines:

```
GET  /api/v1/validation/scenarios          # catalogue with expectations   (eval:read)
POST /api/v1/validation/run                # start a run, returns run_id   (eval:run)
GET  /api/v1/validation/runs               # recent runs                   (eval:read)
GET  /api/v1/validation/runs/{run_id}      # progress and full results     (eval:read)
```

`POST /run` accepts the same filters as the CLI and executes in the background, so a
twenty-scenario run does not sit on an HTTP connection. Poll the run for incremental
results; add `?include_markdown=true` once it completes for the rendered report.

## Reports

`--output` writes Markdown (default) or JSON. The Markdown report contains a summary, a
per-agent breakdown, a scenario index, a dedicated failures section, and per-scenario
detail: the input, the execution and trace identifiers, the tools that ran, approvals with
their decisions, every check with its detail, and the response excerpt. The JSON form is
the same data for pipelines and dashboards.

Because each scenario runs a real execution, the durable evidence is not only the report:
every scenario leaves an execution, a trace, an event log and cost records behind, which
can be opened in the console like any other run.

## Testing the harness itself

A conformance suite that cannot detect a defect is worse than none, so the harness is
tested (`backend/tests/test_validation.py`, 32 tests):

- the catalogue is well-formed — twenty scenarios, unique ids, four per agent, every
  content pattern compiles, the governance paths are covered;
- every assertion fails when it should — wrong status, missing tool, forbidden tool,
  failed tool, missing content, forbidden content, uncited answer, missing approval,
  unexpected approval, approval on the wrong tool, missing guardrail, budget breach,
  missing output key, missing trace;
- the runner drives real executions end to end against a scripted provider: a correct
  answer passes, **a hallucinated answer is caught**, and the CS-04 approval gate is
  suspended, reviewed, resumed and validated.

## Coverage gap

Credit Risk and Collections are implemented but are **not yet in the twenty**. Their models
and controls are covered instead by `backend/tests/test_credit_collections.py`, which tests
them against the published rules directly — the amortisation formula, the Basel III IRB
capital function, the RBI classification ladder and the Fair Practices Code contact window
— and by the rail tests in `backend/tests/test_guardrails.py`. Scenario coverage for both
agents is outstanding work.

## Extending the suite

Add a `Scenario` to `backend/app/validation/scenarios.py`. Prefer structural assertions,
and reserve `must_match` for facts that must be exact — a figure, a deadline, a decision.
Where an agent must refuse, assert both the refusal (`must_match`) and the absence of an
invented answer (`must_not_match`); KA-04 is the pattern to copy.
