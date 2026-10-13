"""Adverse-action reason codes for credit decisions.

A declined applicant is entitled to know why, and "why" has to be a stable, enumerable
reason rather than whatever sentence the model produced that day. `record_credit_decision`
already refused a decline with no reason attached; what it accepted was free text, so two
declines for the same cause could be recorded differently and neither could be counted.

Every code here corresponds to a rule `check_credit_policy` actually evaluates, or to a
decision-stage judgement the underwriting agent makes. Codes are append-only: a recorded
decision keeps its meaning, so retiring a code means marking it rather than reusing it.
"""

from __future__ import annotations

#: Code -> the explanation an applicant is given. Deliberately phrased for the applicant,
#: not for the underwriter: this text is the one that leaves the bank.
ADVERSE_ACTION_REASONS: dict[str, str] = {
    # --- policy knockouts, one per rule in check_credit_policy -----------------
    "AA01": "Applicant is below the minimum age for this product",
    "AA02": "Applicant would exceed the maximum age permitted at the end of the term",
    "AA03": "Identity verification is not complete",
    "AA04": "The application did not clear mandatory financial-crime screening",
    "AA05": "Credit bureau score is below the minimum for this product",
    "AA06": "Existing repayment commitments are too high relative to income",
    "AA07": "Requested term exceeds the maximum for this product",
    "AA08": "Requested amount exceeds the unsecured exposure limit for this customer",
    # --- decision-stage judgements --------------------------------------------
    "AA20": "Income could not be verified from the evidence supplied",
    "AA21": "Affordability assessment shows insufficient disposable income",
    "AA22": "Recent arrears or delinquency on existing facilities",
    "AA23": "Length or depth of credit history is insufficient to assess risk",
    "AA24": "Modelled probability of default is above appetite for this product",
    "AA25": "Supporting documentation was incomplete or inconsistent",
    "AA26": "Declined on review by an underwriter, who recorded the rationale on the case",
}

#: Maps a failed `check_credit_policy` rule onto the reason an applicant is given, so a
#: knockout produces the same code every time rather than a fresh phrasing per run.
POLICY_RULE_TO_REASON: dict[str, str] = {
    "minimum_age": "AA01",
    "age_at_maturity": "AA02",
    "kyc_verified": "AA03",
    "not_sanctioned": "AA04",
    "minimum_bureau_score": "AA05",
    "maximum_foir": "AA06",
    "maximum_tenure": "AA07",
    "unsecured_exposure_cap": "AA08",
}


def unknown_reason_codes(codes: list[str]) -> list[str]:
    """Codes that are not in the published vocabulary, in the order supplied."""
    seen: list[str] = []
    for code in codes:
        normalised = str(code).strip().upper()
        if normalised not in ADVERSE_ACTION_REASONS and normalised not in seen:
            seen.append(normalised)
    return seen


def describe(codes: list[str]) -> list[dict[str, str]]:
    """Pair each code with its applicant-facing explanation, for the decision record."""
    return [
        {"code": c, "reason": ADVERSE_ACTION_REASONS[c]}
        for c in (str(x).strip().upper() for x in codes)
        if c in ADVERSE_ACTION_REASONS
    ]


def reasons_for_failed_rules(rules: list[str]) -> list[str]:
    """The codes implied by a set of failed policy rules, de-duplicated and ordered."""
    out: list[str] = []
    for rule in rules:
        code = POLICY_RULE_TO_REASON.get(rule)
        if code and code not in out:
            out.append(code)
    return out
