# Payment Operations Agent

Payment operations is a control function wearing a service function's clothes. The same
keystroke that fixes a mistyped beneficiary name is the one that commits wire stripping, and
the same button that releases a delayed payment is the one that moves frozen funds. So the
constraints are implemented in the tools, not described in a prompt.

The agent never moves money. It diagnoses, decides, records and instructs; the payment
system settles.

## What it works on

`backend/app/tools/payments.py` — 15 tools over five tables:

| Table | Holds |
|---|---|
| `payment_instructions` | The payment as it sits in the queue: UETR, parties, rail, charge bearer, status, screening status, value date |
| `payment_screening_hits` | A sanctions or watchlist hit on a named party, and its disposition |
| `payment_investigations` | A case on a payment that did not arrive cleanly, with its regulatory deadline |
| `payment_repairs` | Every amendment, with the value before and after and the human who approved it |
| `payment_returns` | A payment sent back, under the ISO reason code it was returned with |

## The standards it implements

**ISO 20022.** A payment carries a **UETR** — the one identifier that survives every hop,
which is what makes a cross-border trace possible at all. `trace_payment` accepts it
interchangeably with the payment reference.

**The charge bearer decides whether a short credit is a defect.** Under `OUR` the debtor
pays every charge and the beneficiary must receive the full amount; under `SHA` and `BEN` a
deduction is the agreed outcome, not an error. The same rule governs how much comes back on
a return: `OUR` returns the full amount, `SHA` and `BEN` retain the charges already spent.

**Return reason codes are the ISO external code set**, not free text — `AC01` incorrect
account number, `AC04` closed account, `AM05` duplication, `BE01` name/account mismatch,
`RC01` bad agent identifier, `RR04` regulatory. `classify_return_reason` derives the code
from the defect the validator actually found, with the evidence attached, so the code the
correspondent receives is one they can act on.

**Identifiers are validated against their published formats**, never eyeballed:

| Check | Standard |
|---|---|
| IBAN | ISO 13616 mod-97-10 check digits |
| BIC | ISO 9362 — 4 alpha institution, 2 alpha country, 2 location, optional 3 branch |
| IFSC | RBI format — 4 alpha bank code, reserved `0`, 6-character branch |
| Currency | ISO 4217 three-letter |
| Charge bearer | ISO 20022 `ChrgBr` |

**Cut-offs and value dating.** `check_cutoff_and_value_date` compares the submission time —
in **bank-local** time, not UTC — against the rail's cut-off and reports the value date that
follows. A great many "late" payments are simply value dated forward, and saying so resolves
the complaint without a case.

**RBI Harmonised Turn Around Time** (DPSS.CO.PD No.629/02.01.014/2019-20). A failed domestic
transaction that is not reversed by T+1 accrues ₹100 per day, **automatically** — whether or
not the customer asked for it. `calculate_compensation` computes it and the agent is
instructed to state it unprompted. Cross-border rails run on the correspondent agreement
instead, and the tool says so rather than inventing a statutory figure.

## The three refusals

These are not policy preferences. Each one is a criminal offence in the jurisdictions this
platform is written for, so each is enforced at the tool layer *and* at the rail — a request
that gets past one still meets the other.

### 1. A party name can never be repaired

`REPAIRABLE_FIELDS` contains routing and narrative fields only. The originator and
beneficiary names are deliberately absent: altering them to get a payment past screening is
**wire stripping**, prosecuted under IEEPA in the US and the equivalent EU regulations. A
payment with the wrong beneficiary is returned and re-originated, never edited.

```
repair_payment(field_name="creditor_name", ...)
→ ValidationError: 'creditor_name' cannot be repaired. Altering a party name on a payment
  in flight is wire stripping; a payment with the wrong beneficiary is returned and
  re-originated instead.
```

### 2. Blocked funds are immovable

A confirmed sanctions match freezes the payment. Every path out of that state refuses:

| Attempt | Result |
|---|---|
| `release_payment` | Refused — blocked by a confirmed match |
| `issue_payment_return` | Refused — returning frozen funds to the originator is itself a breach |
| `repair_payment` | Refused — a blocked payment cannot be amended |
| `classify_return_reason` | Reports `returnable: false` and says why |

### 3. A strong match is not an operator's call

`resolve_screening_hit` refuses a `false_positive` disposition when the match score is at or
above `STRONG_MATCH_THRESHOLD` (0.85) — that hit goes to the sanctions team. Below the
threshold the names are different enough that a documented rationale is a legitimate
disposition, and the tool requires one of at least 20 characters naming what distinguishes
this party from the listed one.

A repair also **invalidates the screening that preceded it**: the payment moves to
`unscreened` and `release_payment` refuses until it has been screened again. Changing the
routing changes who is being paid through whom.

## The rails

`backend/app/guardrails/configs/payment/`

| Side | Rail | Blocks |
|---|---|---|
| input | `banking prompt integrity` | Injection and control bypass |
| input | `wire stripping` | Removing, blanking or falsifying a party; editing a SWIFT field so screening does not see it; "avoid the filter" phrasing |
| input | `blocked funds` | Releasing, returning, refunding or unfreezing a payment behind a match; dismissing a hit in order to pay; skipping screening, dual authorisation or maker-checker |
| input | `self check input` | Model-judged: backdating a value date, recording a settlement that did not occur, under-stating compensation owed |
| output | `wire stripping output` / `blocked funds output` | The same proposals appearing in the answer |
| output | `sensitive disclosure` | Identifier masking |
| output | `self check output` | Model-judged: a figure no tool produced, a SHA deduction reported as a defect, omitted compensation |

The false-positive half is tested as carefully as the blocking half: tracing, validating,
choosing a reason code, repairing a routing field, computing compensation and clearing a
weak match with a rationale all pass. See `TestPaymentRails` in
`backend/tests/test_payments.py`.

## Conformance scenarios

| ID | Proves |
|---|---|
| PAY-01 | A stalled payment is traced before a case is opened — most "missing" payments are visible in the trace |
| PAY-02 | An invalid IBAN produces `AC01`, the code the evidence supports |
| PAY-03 | Harmonised-TAT compensation is computed and stated without being asked for |
| PAY-04 | Wire stripping is refused before any tool touches the payment (`wire_stripping` rail named explicitly) |

## The seeded book

`seed-banking` loads a queue an operator would find on a Monday morning. Every payment
exists so a specific control has something to act on:

| Reference | Why it is there |
|---|---|
| PAY-100001 | Clean, settled, `OUR` charges — the baseline, and the full-amount return case |
| PAY-100002 | Held on a **weak** name match — the false-positive path |
| PAY-100003 | Held on an **exact** listed name — the block-and-never-release path |
| PAY-100004 | Valid-looking IBAN that fails the check digits — the repair and `AC01` path |
| PAY-100005 / PAY-100006 | Identical debtor, creditor, amount and currency — the `AM05` duplicate path |
| PAY-100007 | Failed IMPS, six days unreversed against a T+1 deadline — the compensation clock |

## Running it

```bash
cd backend
.venv/bin/python -m app.cli init-db && .venv/bin/python -m app.cli seed-banking
.venv/bin/python -m app.cli run-agent payment \
  --input '{"query": "Where is this payment and what should we do?", "payment": "PAY-100002"}'
.venv/bin/pytest tests/test_payments.py -q
.venv/bin/python -m app.cli validate --agent payment
```
