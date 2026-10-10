# EU AI Act compliance pack

Annex III, point 5(b) of the EU AI Act classifies a system used to evaluate the
creditworthiness of a natural person as high risk. The Credit Risk agent does exactly that,
which brings Articles 9 to 15 and the Annex IV documentation duty onto this platform.

Most of the obligations were already met by the engine: a human decides before any
consequential write, every run leaves a trace, and the conformance suite proves behaviour
against records rather than against the model's self-report. Three things were not, and this
pack is those three.

| Obligation | Article | What was missing | Where it lives now |
|---|---|---|---|
| Explanation to the affected person | 13, 86 | A decline carried free-text reasons | `backend/app/compliance/adverse_action.py` |
| Evidenced accuracy across groups | 15 | Nothing measured disparity | `backend/app/compliance/fairness.py` |
| Technical documentation, kept current | 11, Annex IV | No Annex IV artefact | `backend/app/compliance/annex_iv.py` |

## Adverse-action reason codes

`record_credit_decision` already refused a decline with no reason attached. What it accepted
was free text, so two declines for the same cause could be recorded differently and neither
could be counted — and the sentence that left the bank was whatever the model phrased that
run.

Reasons now come from a published vocabulary of fifteen codes. Codes `AA01`–`AA08`
correspond one-for-one to the rules `check_credit_policy` evaluates, so a policy knockout
produces the same code every time; `AA20`–`AA26` cover the judgements made at the decision
stage. Each code carries the applicant-facing wording, because that text is the one that
leaves the bank.

- An unrecognised code is rejected at the tool boundary, before the application is even
  looked up, with the valid set in the error.
- Codes are normalised (`" aa01 "` → `AA01`) before they are judged: casing is transport
  noise, not a different reason.
- The decision payload returns `adverse_action_reasons` — code and wording paired — so the
  letter can be generated from the record instead of from a second list.
- The vocabulary is append-only. A recorded decision keeps its meaning, so retiring a code
  means marking it, never reusing it.

`reasons_for_failed_rules()` maps a set of failed policy rules onto the codes they imply.
A test asserts that every mapped rule is still evaluated by the policy, so a rule renamed in
the credit tools breaks the suite rather than silently producing declines nobody can explain.

## Fairness measurement

A conformance suite that proves an agent reaches the right answer on chosen records does not
answer the separate question of whether it reaches different answers for different groups.

`measure()` computes the selection rate — the share of decisions that were approvals — for
each group of a protected attribute, and the adverse impact ratio against the most favoured
comparable group. The four-fifths convention treats a ratio below 0.80 as worth
investigating. It is a screening threshold, not a verdict: a disparity can have a lawful
explanation, and reporting the number is what makes the question answerable at all.

Decisions that matter to the arithmetic:

- **A referral is not an approval.** Only `approve` counts. Folding referrals into either
  side would flatter or penalise whichever group gets referred most.
- **Small groups are measured but excluded from the ratio.** A selection rate over three
  applications says nothing, and letting it set the reference point manufactures a disparity
  that is an artefact of the sample. The floor is 20 decisions.
- **One comparable group is `inconclusive`, not fair.** With nothing to compare against,
  "no disparity detected" would be an unearned claim.
- **Age is banded, not raw.** A selection rate over single years of age is noise; age is the
  protected characteristic, not the birthday. The band is the age at the reference date, so
  a birthday that has not happened yet does not move anyone up a band.

### What is measured, and what is not

`measure_credit_decisions()` reads `credit_decisions` joined to the customer who was decided
on, and reports `age_band` (derived from the KYC date of birth) and `nationality`.

Sex, ethnicity and disability are **not** measured, because the platform does not collect
them. They are named in every report with that reason rather than omitted. Inferring them
from a name or a postcode to produce a fairness number would create a special-category
inference the applicant never consented to — a worse outcome than the gap it closes.

### Where it surfaces

Every conformance run carries a `fairness` section: a table per attribute in the Markdown
report, a line per attribute in the console summary, and a `fairness` key in the JSON.

It deliberately does **not** move the conformance verdict. A release gate that silently
turned amber on a sample artefact would be worse than one that makes the operator read the
number, and a measurement that could fail a build is a measurement people learn to game.

## Annex IV technical documentation

```bash
python -m app.cli annex-iv --output docs/generated/annex-iv.md
python -m app.cli annex-iv --format json        # same facts, for a pipeline
```

Article 11 requires technical documentation covering the nine headings of Annex IV, kept
current. Written by hand it is stale the day after it is signed: it describes the agents,
tools and controls the author could remember.

So it is generated. The agent registry, the tool registry and its human-review predicate,
the guardrail configurations on disk, the conformance suite, the adverse-action vocabulary
and the fairness attributes are all read live. A tool added without an approval gate, or an
agent registered without an owner, appears as a gap instead of going unmentioned.

The document covers:

- **1** — the intended purpose and accountable owner of each implemented agent, and which
  agents are Annex III high risk and on what basis;
- **2** — the execution graph as the control surface, the full tool inventory with the gate
  each one is subject to, the recorded exemptions for ungated writes with the reason for
  each, the guardrail configuration files in force, and conformance coverage per agent;
- **3** — what the audit trail records, the retention period, and the adverse-action
  vocabulary as the Article 13 transparency measure;
- **4** — the fairness methodology and the attributes that cannot be measured;
- **7** — the frameworks actually applied as the control baseline.

### What it refuses to do

It does not assert conformity.

No harmonised standard under Article 40 covered this system when the generator was written,
so it claims none; it lists ISO/IEC 42001, ISO/IEC 23894, the NIST AI RMF and the OWASP LLM
Top 10 as *applied*, which is a different and true statement.

The headings only a person can answer are emitted as **outstanding**, each naming the role
that owes it: the Article 9 risk register and residual-risk acceptance (CRO), the
appropriateness of the accuracy metrics (Head of Credit Risk Modelling), the signed
declaration of conformity (Compliance), and the Article 72 post-market monitoring plan (Head
of Operational Risk). A generator that quietly filled those in would be worse than no
document, because it would read as though someone had.

Generated against an unseeded database it says so, loudly, rather than reporting that no
agent is high risk — true of the empty registry it read, and the opposite of what a reader
would take it to mean.

### Gaps are a non-zero exit

The command exits `1` when it finds a gap in what is deployed — an implemented agent with no
owner email, no guardrail configuration or no conformance scenario, or a data-writing tool
with neither a review gate nor a recorded exemption. The document is still written, so a
pipeline can publish the file and fail the build in the same step: a gap the operator cannot
read is a gap nobody closes.

## Tests

`backend/tests/test_compliance.py`, 34 tests, with no provider needed:

- the vocabulary is usable — every policy rule maps to a published code, the codes are
  distinct, every mapped rule is still evaluated by the credit policy, and an invented code
  is refused by the tool;
- the fairness arithmetic is right where it matters — a clear disparity is flagged against
  the most favoured group, 0.82 is not flagged, a referral is not an approval, a group of
  three cannot set the reference point, one group is inconclusive, a never-approved group
  reports 0.0 rather than "not measured", and ungrouped records are skipped rather than
  bucketed;
- the measurement reaches the report — flagged groups appear in the Markdown and the console
  summary, a clean result is still reported, and the verdict does not move;
- the generator earns its keep — the tool inventory reports the same gate the engine
  consults, an agent registered without an owner email is reported as a gap, no harmonised
  standard is claimed, and every outstanding heading names its owner.
