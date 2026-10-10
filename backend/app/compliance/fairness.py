"""Fairness measurement over recorded credit decisions.

Annex III of the EU AI Act treats credit scoring of natural persons as high risk, and
Article 15 asks for accuracy and robustness that can be evidenced rather than asserted. A
conformance suite that proves an agent reaches the right answer on chosen records does not
answer the separate question of whether it reaches different answers for different groups.

This measures the selection rate -- the share of decisions that were approvals -- for each
group of a protected attribute, and reports the adverse impact ratio against the most
favoured group. The four-fifths convention treats a ratio below 0.8 as worth investigating;
it is a screening threshold, not a verdict, and a disparity can have a lawful explanation.
Reporting it is what makes the question answerable at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

#: Below this, the convention says look closer. It is not a pass mark and nothing in this
#: module treats it as one -- the caller decides what an explained disparity means.
FOUR_FIFTHS = 0.8

#: Bands rather than raw ages: a selection rate over single years of age is noise, and age
#: is the protected characteristic, not the birthday.
AGE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("18-24", 18, 24),
    ("25-34", 25, 34),
    ("35-44", 35, 44),
    ("45-54", 45, 54),
    ("55-64", 55, 64),
    ("65+", 65, 200),
)


def age_band(born: date | None, *, today: date | None = None) -> str | None:
    if born is None:
        return None
    now = today or date.today()
    years = now.year - born.year - ((now.month, now.day) < (born.month, born.day))
    for label, low, high in AGE_BANDS:
        if low <= years <= high:
            return label
    return None


@dataclass
class GroupOutcome:
    group: str
    decisions: int = 0
    approvals: int = 0

    @property
    def selection_rate(self) -> float:
        return self.approvals / self.decisions if self.decisions else 0.0


@dataclass
class FairnessReport:
    attribute: str
    groups: list[GroupOutcome] = field(default_factory=list)
    #: Groups with fewer decisions than this are measured but excluded from the ratio: a
    #: selection rate over three applications says nothing, and letting it set the
    #: reference point produces a disparity that is an artefact of the sample.
    minimum_group_size: int = 20

    @property
    def comparable(self) -> list[GroupOutcome]:
        return [g for g in self.groups if g.decisions >= self.minimum_group_size]

    @property
    def reference(self) -> GroupOutcome | None:
        return max(self.comparable, key=lambda g: g.selection_rate, default=None)

    def to_dict(self) -> dict[str, Any]:
        reference = self.reference
        best = reference.selection_rate if reference else 0.0
        rows = []
        for g in sorted(self.groups, key=lambda x: x.group):
            comparable = g.decisions >= self.minimum_group_size
            ratio = (g.selection_rate / best) if (comparable and best) else None
            rows.append(
                {
                    "group": g.group,
                    "decisions": g.decisions,
                    "approvals": g.approvals,
                    "selection_rate": round(g.selection_rate, 4),
                    "adverse_impact_ratio": round(ratio, 4) if ratio is not None else None,
                    "below_four_fifths": bool(ratio is not None and ratio < FOUR_FIFTHS),
                    "comparable": comparable,
                }
            )
        flagged = [r["group"] for r in rows if r["below_four_fifths"]]
        return {
            "attribute": self.attribute,
            "reference_group": reference.group if reference else None,
            "minimum_group_size": self.minimum_group_size,
            "groups": rows,
            "groups_below_four_fifths": flagged,
            # Explicitly inconclusive rather than "fair": with one comparable group there
            # is nothing to compare, and saying so beats reporting a ratio of 1.0.
            "status": "inconclusive"
            if len(self.comparable) < 2
            else ("review" if flagged else "no_disparity_detected"),
        }


def measure(
    attribute: str,
    observations: list[tuple[str | None, str]],
    *,
    minimum_group_size: int = 20,
) -> FairnessReport:
    """Build a report from `(group, decision)` pairs; `None` groups are skipped.

    A decision is counted as an approval when it is exactly "approve": a referral is not a
    grant, and folding it into either side would flatter or penalise whichever group gets
    referred most.
    """
    report = FairnessReport(attribute=attribute, minimum_group_size=minimum_group_size)
    index: dict[str, GroupOutcome] = {}
    for group, decision in observations:
        if not group:
            continue
        outcome = index.setdefault(group, GroupOutcome(group=group))
        outcome.decisions += 1
        if decision == "approve":
            outcome.approvals += 1
    report.groups = list(index.values())
    return report


#: The attributes this platform can actually measure. Gender and ethnicity are not collected
#: anywhere in the data model, and inferring them from a name to produce a fairness number
#: would create a special-category inference the applicant never consented to. Age is
#: derived from the date of birth the KYC record already holds; nationality is stored
#: outright. Absent attributes are named in the report so the gap is on the record.
MEASURABLE_ATTRIBUTES: tuple[str, ...] = ("age_band", "nationality")
UNMEASURABLE_ATTRIBUTES: dict[str, str] = {
    "sex": "not collected by the platform; not inferred",
    "ethnicity": "not collected by the platform; not inferred",
    "disability": "not collected by the platform; not inferred",
}


async def measure_credit_decisions(session: Any, *, minimum_group_size: int = 20) -> list[FairnessReport]:
    """Measure every available protected attribute over the recorded credit decisions.

    Reads `credit_decisions` joined to the customer who was decided on. The join is on the
    customer id the decision stored rather than on the application, so a decision survives
    the application being reshaped.
    """
    from sqlalchemy import select

    from app.db.models.banking import CreditDecision, Customer

    rows = (
        await session.execute(
            select(CreditDecision.decision, Customer.date_of_birth, Customer.nationality).join(
                Customer, Customer.id == CreditDecision.customer_id
            )
        )
    ).all()

    by_attribute: dict[str, list[tuple[str | None, str]]] = {a: [] for a in MEASURABLE_ATTRIBUTES}
    for decision, born, nationality in rows:
        by_attribute["age_band"].append((age_band(born), decision))
        by_attribute["nationality"].append((nationality or None, decision))

    return [
        measure(attribute, observations, minimum_group_size=minimum_group_size)
        for attribute, observations in by_attribute.items()
    ]
