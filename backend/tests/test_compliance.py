"""The EU AI Act credit compliance pack: reason codes, fairness, Annex IV.

Credit scoring of natural persons is Annex III high risk, so these three controls are the
difference between a platform that can evidence Articles 13-15 and one that asserts them.
Each test here is about the control doing its job, not about the shape of a dict.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.compliance.adverse_action import (
    ADVERSE_ACTION_REASONS,
    POLICY_RULE_TO_REASON,
    describe,
    reasons_for_failed_rules,
    unknown_reason_codes,
)
from app.compliance.annex_iv import HIGH_RISK_AGENTS, _tool_inventory, to_markdown
from app.compliance.fairness import (
    FOUR_FIFTHS,
    UNMEASURABLE_ATTRIBUTES,
    age_band,
    measure,
)


class TestTheReasonVocabularyIsUsable:
    def test_every_policy_rule_maps_to_a_published_reason(self):
        """A knockout the applicant cannot be told about is a knockout we cannot use."""
        for rule, code in POLICY_RULE_TO_REASON.items():
            assert code in ADVERSE_ACTION_REASONS, f"{rule} maps to unpublished code {code}"

    def test_the_mapped_codes_are_distinct(self):
        """Two rules sharing a code would make the decline reason ambiguous on the record."""
        codes = list(POLICY_RULE_TO_REASON.values())
        assert len(codes) == len(set(codes))

    def test_every_rule_check_credit_policy_evaluates_has_a_reason(self):
        """Guards the real coupling: a rule added to the policy with no applicant wording."""
        from app.tools import credit as credit_tools

        source = __import__("inspect").getsource(credit_tools)
        # The policy evaluates rules by name; each name that appears as a rule key must be
        # mappable, otherwise a decline on it cannot be explained.
        for rule in POLICY_RULE_TO_REASON:
            assert rule in source, f"{rule} is mapped but no longer evaluated"

    def test_unknown_codes_are_reported_in_order_without_duplicates(self):
        assert unknown_reason_codes(["AA01", "ZZ9", "nope", "ZZ9"]) == ["ZZ9", "NOPE"]

    def test_codes_are_normalised_before_they_are_judged(self):
        """Casing and whitespace are transport noise, not a different reason."""
        assert unknown_reason_codes([" aa01 "]) == []
        assert describe([" aa01 "])[0]["code"] == "AA01"

    def test_describe_attaches_the_applicant_facing_wording(self):
        described = describe(["AA05", "AA06"])
        assert [d["code"] for d in described] == ["AA05", "AA06"]
        assert all(d["reason"] for d in described)

    def test_failed_rules_become_codes_once_each(self):
        codes = reasons_for_failed_rules(["minimum_bureau_score", "maximum_foir", "minimum_bureau_score"])
        assert codes == ["AA05", "AA06"]

    def test_an_unrecognised_rule_contributes_nothing_rather_than_guessing(self):
        assert reasons_for_failed_rules(["some_new_rule"]) == []


class TestTheDecisionToolRejectsUnpublishedCodes:
    @pytest.mark.asyncio
    async def test_a_decline_with_an_invented_reason_code_is_refused(self, session):
        """The point of the vocabulary: free text cannot get back in through the tool."""
        import app.tools  # noqa: F401
        from app.core.errors import ValidationError
        from app.tools import registry
        from app.tools.base import ToolContext

        tool_context = ToolContext(session=session, user_email="tester@finops.local")
        tool = registry.get("record_credit_decision")
        with pytest.raises(ValidationError) as caught:
            await tool.handler(
                tool.args_model(
                    application="LA-UNUSED",
                    decision="decline",
                    reason_codes=["BECAUSE_THE_MODEL_SAID_SO"],
                ),
                tool_context,
            )
        assert "BECAUSE_THE_MODEL_SAID_SO" in str(caught.value.details["unknown"])


class TestAgeBanding:
    def test_the_band_is_the_age_at_the_reference_date_not_the_birth_year(self):
        """A birthday that has not happened yet does not make someone a year older."""
        born = date(2001, 12, 31)
        assert age_band(born, today=date(2026, 12, 30)) == "18-24"
        assert age_band(born, today=date(2026, 12, 31)) == "25-34"

    def test_a_missing_date_of_birth_yields_no_band_rather_than_a_default(self):
        """Bucketing unknown ages into one band would invent a group and then measure it."""
        assert age_band(None) is None

    def test_someone_below_the_lowest_band_is_not_banded(self):
        assert age_band(date(2020, 1, 1), today=date(2026, 1, 1)) is None

    def test_the_bands_are_contiguous_and_non_overlapping(self):
        from app.compliance.fairness import AGE_BANDS

        for (_, _, high), (_, low, _) in zip(AGE_BANDS, AGE_BANDS[1:], strict=False):
            assert low == high + 1


class TestFairnessMeasurement:
    def test_a_clear_disparity_is_flagged_against_the_most_favoured_group(self):
        observations = [("young", "approve")] * 50 + [("young", "decline")] * 50
        observations += [("old", "approve")] * 10 + [("old", "decline")] * 90
        report = measure("age_band", observations).to_dict()

        assert report["reference_group"] == "young"
        rows = {r["group"]: r for r in report["groups"]}
        assert rows["young"]["selection_rate"] == 0.5
        assert rows["old"]["adverse_impact_ratio"] == 0.2
        assert rows["old"]["below_four_fifths"] is True
        assert report["groups_below_four_fifths"] == ["old"]
        assert report["status"] == "review"

    def test_a_ratio_just_above_four_fifths_is_not_flagged(self):
        """The threshold is a screening line; a report that flagged 0.81 would be noise."""
        observations = [("a", "approve")] * 100 + [("a", "decline")] * 100
        observations += [("b", "approve")] * 82 + [("b", "decline")] * 118
        rows = {r["group"]: r for r in measure("x", observations).to_dict()["groups"]}
        assert rows["b"]["adverse_impact_ratio"] == pytest.approx(0.82)
        assert rows["b"]["below_four_fifths"] is False

    def test_a_referral_is_not_counted_as_an_approval(self):
        """Folding referrals into either side flatters whichever group gets referred most."""
        report = measure("x", [("a", "approve"), ("a", "refer"), ("a", "decline")], minimum_group_size=1)
        row = report.to_dict()["groups"][0]
        assert row["decisions"] == 3
        assert row["approvals"] == 1

    def test_a_group_below_the_minimum_size_cannot_set_the_reference_point(self):
        """Otherwise a lucky run of three approvals manufactures a disparity everywhere else."""
        observations = [("tiny", "approve")] * 3
        observations += [("real", "approve")] * 40 + [("real", "decline")] * 60
        report = measure("x", observations, minimum_group_size=20)

        assert report.reference is not None
        assert report.reference.group == "real"
        rows = {r["group"]: r for r in report.to_dict()["groups"]}
        assert rows["tiny"]["comparable"] is False
        assert rows["tiny"]["adverse_impact_ratio"] is None
        assert rows["real"]["below_four_fifths"] is False

    def test_one_comparable_group_is_inconclusive_not_fair(self):
        """With nothing to compare against, "no disparity" would be an unearned claim."""
        report = measure("x", [("only", "approve")] * 30).to_dict()
        assert report["status"] == "inconclusive"
        assert report["groups_below_four_fifths"] == []

    def test_no_decisions_at_all_is_inconclusive(self):
        report = measure("x", []).to_dict()
        assert report["status"] == "inconclusive"
        assert report["groups"] == []

    def test_records_with_no_group_are_skipped_rather_than_bucketed(self):
        report = measure("x", [(None, "approve"), ("", "approve"), ("a", "approve")], minimum_group_size=1)
        assert [r["group"] for r in report.to_dict()["groups"]] == ["a"]

    def test_a_group_that_was_never_approved_reports_a_ratio_of_zero(self):
        """Zero is the honest answer; `None` would read as "not measured"."""
        observations = [("a", "approve")] * 30 + [("b", "decline")] * 30
        rows = {r["group"]: r for r in measure("x", observations).to_dict()["groups"]}
        assert rows["b"]["adverse_impact_ratio"] == 0.0
        assert rows["b"]["below_four_fifths"] is True

    def test_four_fifths_is_the_published_convention(self):
        assert FOUR_FIFTHS == 0.8

    def test_attributes_we_do_not_hold_are_named_rather_than_inferred(self):
        """The gap belongs on the record: inferring sex from a name is not a fairness metric."""
        assert set(UNMEASURABLE_ATTRIBUTES) >= {"sex", "ethnicity"}
        assert all(reason for reason in UNMEASURABLE_ATTRIBUTES.values())


class TestFairnessOverRecordedDecisions:
    @pytest.mark.asyncio
    async def test_it_measures_every_available_attribute_over_the_decisions_on_record(self, session):
        from app.compliance.fairness import MEASURABLE_ATTRIBUTES, measure_credit_decisions

        reports = await measure_credit_decisions(session)
        assert [r.attribute for r in reports] == list(MEASURABLE_ATTRIBUTES)
        # An empty deployment must say "inconclusive" rather than "no disparity detected".
        for report in reports:
            assert report.to_dict()["status"] in {"inconclusive", "review", "no_disparity_detected"}


class TestTheConformanceReportCarriesTheMeasurement:
    @staticmethod
    def _report(fairness: list[dict]) -> object:
        from datetime import UTC, datetime

        from app.validation.runner import ValidationReport

        now = datetime.now(UTC)
        return ValidationReport(started_at=now, finished_at=now, results=[], preflight={}, fairness=fairness)

    def test_a_flagged_group_reaches_the_markdown_report(self):
        from app.validation.report import to_markdown

        observations = [("25-34", "approve")] * 50 + [("25-34", "decline")] * 50
        observations += [("65+", "approve")] * 5 + [("65+", "decline")] * 95
        fairness = [measure("age_band", observations).to_dict()]

        document = to_markdown(self._report(fairness))
        assert "Fairness across protected groups" in document
        assert "`65+`" in document
        assert "REVIEW" in document
        # The attributes we cannot measure are named in the same section, every run.
        assert "Attributes not measured" in document
        assert "ethnicity" in document

    def test_the_console_summary_names_the_flagged_groups(self):
        from app.validation.report import to_console

        observations = [("a", "approve")] * 40 + [("b", "decline")] * 40
        output = to_console(self._report([measure("x", observations).to_dict()]))
        assert "fairness" in output
        assert "groups below four-fifths: b" in output

    def test_a_clean_measurement_is_still_reported(self):
        """A section that only appears on a bad result reads as an accusation."""
        from app.validation.report import to_markdown

        observations = [("a", "approve")] * 30 + [("b", "approve")] * 30
        document = to_markdown(self._report([measure("x", observations).to_dict()]))
        assert "Fairness across protected groups" in document
        assert "no disparity detected" in document

    def test_a_measurement_does_not_change_the_conformance_verdict(self):
        """A screening threshold must not silently turn a green release amber."""
        observations = [("a", "approve")] * 40 + [("b", "decline")] * 40
        report = self._report([measure("x", observations).to_dict()])
        assert report.ok is True


class TestAnnexIvDocumentation:
    def test_the_credit_agent_is_classified_against_the_annex_that_covers_it(self):
        assert "credit_risk" in HIGH_RISK_AGENTS
        assert "5(b)" in HIGH_RISK_AGENTS["credit_risk"]

    def test_the_tool_inventory_reports_the_gate_the_engine_actually_consults(self):
        """Documentation drawn from a second list would drift from the enforced one."""
        import app.tools  # noqa: F401
        from app.tools import registry

        inventory = {row["name"]: row for row in _tool_inventory()}
        assert inventory
        for tool in registry.all():
            assert inventory[tool.name]["requires_human_review"] == tool.requires_human_review

    def test_every_write_without_a_gate_carries_a_recorded_reason(self):
        import app.tools  # noqa: F401
        from app.tools.base import REVIEW_EXEMPT_WRITES

        for row in _tool_inventory():
            if row["writes_data"] and not row["requires_human_review"]:
                assert REVIEW_EXEMPT_WRITES.get(row["name"]), row["name"]

    @pytest.mark.asyncio
    async def test_the_document_names_its_gaps_instead_of_claiming_conformity(self, session):
        import app.tools  # noqa: F401
        from app.compliance.annex_iv import collect

        facts = await collect(session)
        document = to_markdown(facts)

        assert "is **not** a declaration of conformity" in document
        assert "Outstanding sections" in document
        # Every heading only a person can answer must name the role that owes it.
        for owed in facts["human_owed_sections"]:
            assert owed["owner"] in document
            assert owed["annex_iv"] in document

    @pytest.mark.asyncio
    async def test_an_agent_without_an_owner_email_is_reported_as_a_gap(self, session):
        """The generator earns its keep by finding what the registry is missing."""
        from sqlalchemy import select

        import app.tools  # noqa: F401
        from app.compliance.annex_iv import collect
        from app.db.models.agents import Agent

        session.add(
            Agent(
                key="unowned_agent",
                name="Unowned",
                description="Registered without an accountable owner",
                availability="implemented",
                owner="Nobody",
                owner_email=None,
            )
        )
        await session.flush()
        assert (
            await session.execute(select(Agent).where(Agent.key == "unowned_agent"))
        ).scalar_one() is not None

        gaps = (await collect(session))["gaps"]
        assert any("unowned_agent" in g["gap"] and g["annex_iv"] == "1(a)" for g in gaps)

    @pytest.mark.asyncio
    async def test_no_harmonised_standard_is_claimed(self, session):
        """Article 40 standards were still in drafting; citing one would be a false claim."""
        import app.tools  # noqa: F401
        from app.compliance.annex_iv import collect

        document = to_markdown(await collect(session))
        assert "No harmonised standard under Article 40 covers this system yet" in document
