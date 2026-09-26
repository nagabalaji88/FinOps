# Credit Risk and Collections

Two agents over the lending book: one decides whether to lend, the other works the accounts
that fall behind. They share the data model, and both make decisions a regulator can ask
the bank to justify years later — so the models and the constraints are implemented, not
described.

## Credit Risk Agent

Underwrites a retail credit application end to end and recommends a decision. **A human
credit officer decides**; `record_credit_decision` suspends the run for approval.

### The sequence

| Step | Tool | What it produces |
|---|---|---|
| 1 | `get_credit_application` | The application, the applicant, and their existing exposure across loans and cards |
| 2 | `pull_credit_bureau` | The bureau record, with its age. A stale or missing pull stops the assessment |
| 3 | `assess_affordability` | FOIR against **verified** income — salary credits in the customer's own ledger, not the declared figure |
| 4 | `score_credit_risk` | Application score, PD and grade, with every characteristic's contribution |
| 5 | `estimate_loss_given_default` | LGD from collateral cover after regulatory haircuts |
| 6 | `calculate_expected_loss` | EL and Basel III IRB capital |
| 7 | `price_facility` | The rate, built up line by line |
| 8 | `check_credit_policy` | The hard rules |
| 9 | `recommend_limit` | The sanctionable amount and the constraint that binds it |
| 10 | `record_credit_decision` | The decision — **approval-gated** |

Plus `evaluate_covenants` for post-disbursement monitoring and
`summarise_credit_portfolio` for the book-level view.

### The models

**Logistic scorecard**, the industry standard for retail origination, calibrated at 600
points = 50:1 good:bad odds, doubling every 40 points:

```
score = offset + factor · ln(odds)        factor = PDO / ln 2
```

Eight characteristics — bureau score, worst delinquency in 24 months, revolving
utilisation, enquiries, history length, employment stability, FOIR, write-offs — each
banded into points, plus an employment-type adjustment. A missing characteristic scores its
own penalty and is named in `missing_characteristics` rather than silently treated as
neutral.

**FOIR** (fixed obligation to income ratio) is the RBI's affordability measure:

```
FOIR = (existing obligations + proposed instalment) / net monthly income
```

Income is verified against salary credits over 180 days; where the verified figure differs
from the declared one, both are returned along with the variance, and the verified figure
is used.

**Basel III IRB** capital for retail "other" exposures — the prescribed formula, not a
lookup table:

```
R = 0.03·(1−e^−35·PD)/(1−e^−35) + 0.16·[1 − (1−e^−35·PD)/(1−e^−35)]
K = LGD · [ N( (N⁻¹(PD) + √R · N⁻¹(0.999)) / √(1−R) ) − PD ]
RWA = K · 12.5 · EAD
```

with the 0.03% regulatory PD floor applied.

**LGD** starts at the foundation-IRB senior unsecured 45% and falls with collateral cover
after a haircut per collateral type; unrecognised collateral takes the most conservative
haircut rather than being credited.

**Pricing** is a build-up, so a rate is explainable line by line:

```
rate = cost of funds + operating cost + expected loss + capital charge
```

clamped to the product floor and ceiling, with the premium to the indicative rate reported.

### Rules the agent cannot talk its way past

- **A policy knockout is final.** Age at maturity, KYC status, sanctions flag, tenure,
  minimum bureau score, FOIR cap and unsecured exposure cap are hard rules. A good score
  never overrides one.
- **`check_credit_policy` reads the bureau itself** rather than trusting the caller to pass
  a score. A check that silently fails because an argument was omitted is not a check.
- **A decline must carry reason codes**; the tool refuses one that does not.
- **An approval must carry a sanctioned amount.**
- **Protected characteristics are refused by the rails**, on the way in and on the way out —
  see below.

## Collections Agent

Works past-due accounts inside the RBI Fair Practices Code. It never contacts anybody: it
decides, records and schedules, and the channel systems deliver.

| Tool | What it does |
|---|---|
| `scan_delinquent_accounts` | Sweeps loans and cards, classifies arrears, opens cases |
| `get_delinquency_case` / `calculate_arrears` | The live position, bucket, classification, provisioning and roll-rate risk |
| `score_collectability` | Recovery likelihood from this customer's own behaviour |
| `recommend_treatment` | The ladder for the bucket, minus whatever the controls forbid |
| `check_contact_eligibility` | Consent, cease-contact, dispute, permitted hours, frequency |
| `assess_hardship` | Affordability, with a reserve the customer keeps |
| `log_contact_attempt` / `record_promise_to_pay` / `evaluate_promise_performance` | The record |
| `create_repayment_plan` | Restructure — **approval-gated** |
| `escalate_to_recovery` | Legal recovery — **approval-gated, critical** |
| `collections_portfolio_summary` | Buckets, provisioning, promise performance |

### RBI asset classification

Implemented as the ladder, including the special-mention sub-grades that sit below the NPA
boundary:

| Days past due | Bucket | Classification | Provision (secured / unsecured) |
|---|---|---|---|
| 0 | X | standard | 0.4% |
| 1–30 | 0 | standard **SMA-0** | 0.4% |
| 31–60 | 1 | standard **SMA-1** | 0.4% |
| 61–90 | 2 | standard **SMA-2** | 0.4% |
| 91–455 | 3–4 | sub-standard | 15% / 25% |
| 456–1185 | 5+ | doubtful | 40% / 100% |
| 1186+ | 5+ | loss | 100% |

Ninety days is the last standard day; 91 is non-performing.

### The controls, and why they win

A collections strategy is a ladder, and the account's controls cut rungs out of it:

| Control | Effect |
|---|---|
| `cease_contact` | Every live channel is suppressed. There is no "later" — `next_eligible_at` is null |
| `contact_consent` withdrawn | SMS, email and calls are suppressed |
| `dispute_open` | Only written correspondence; legal notice, recovery, settlement and field visits are suppressed |
| `hardship_flag` | Legal notice, recovery referral and field visits are suppressed |

`recommend_treatment` never unlocks an action from a later bucket, and never returns one a
control forbids. `escalate_to_recovery` refuses outright while a dispute or hardship
arrangement is live, or while the account is not yet non-performing.

**The contact window is a local-time rule.** The Fair Practices Code 08:00–19:00 window is
evaluated in the bank's operating timezone (`BANK_TIMEZONE`, default `Asia/Kolkata`), not
in UTC — comparing against UTC would have wrongly blocked every call in an Indian bank's
working day. Letters are not time-restricted. Frequency is capped at one attempt per day,
three per week, with a 24-hour minimum interval.

**Hardship is affordability-first.** `assess_hardship` reserves 15% of net income for the
customer before any surplus is available for a plan, and `create_repayment_plan` refuses an
instalment above that surplus. Where nothing is affordable the tool says so and recommends
referral for concession or settlement — an unaffordable plan is a worse outcome than no
plan.

## Guardrails

Both agents carry NeMo rail configurations
(`backend/app/guardrails/configs/{credit_risk,collections}/`).

**Credit Risk — fair lending.** Input and output rails refuse any reasoning that turns on a
protected characteristic: age beyond the policy limits, sex, marital status, pregnancy,
religion, caste, race, ethnicity, disability, nationality or the applicant's neighbourhood.
Redlining language is refused. The self-check output rail additionally blocks a decline
without actionable reason codes, a figure no tool produced, and an approval over a
knockout.

**Collections — conduct.** Input and output rails refuse threats of arrest, criminal
proceedings or public disclosure; any suggestion of contacting an employer, relative,
neighbour or other third party about the debt; and any request to work around a
cease-contact instruction, withdrawn consent, open dispute or hardship arrangement.

Rails fail closed, as everywhere else — see [`GUARDRAILS.md`](GUARDRAILS.md).

## Sample data

`python -m app.cli seed-banking` provisions both agents:

- **bureau records** for every customer, and **five credit applications** chosen so
  underwriting has a clean approve, a secured approve, a high-FOIR breach, a
  below-minimum-score decline and a thin-file case;
- a **delinquency band** of customers whose loans carry a fixed spread of days past due
  (12, 40, 75, 130, 220), one per collections bucket, so every seeded environment has a
  case in each stage;
- **salary credits and loan repayments** in the ledger, because affordability verification
  and promise performance both read the ledger rather than a declared figure — the
  applicants' salary credits match what they declared, so a variance in the output means a
  real variance.

The delinquency band sits after the applicants so a forced facility never distorts an
application profile, and the scarcest contact controls (cease-contact, open dispute,
withdrawn consent) are assigned first so even a small seeded book exercises each one.

## Testing

`backend/tests/test_credit_collections.py` — 62 tests, written against the published rules
rather than the implementation: the amortisation formula against independently computed
values, the scorecard's points-to-double-the-odds property, the Basel correlation band and
its decay, linearity in LGD, the regulatory PD floor, the RBI ladder at every boundary,
the contact window across the day in local time, and every refusal the tools must make.

```bash
cd backend && .venv/bin/pytest tests/test_credit_collections.py -q
```
