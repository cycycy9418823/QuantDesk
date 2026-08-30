"""Guard the additive ``unrestricted_100k`` paper scenario.

The scenario carries the same 100k paper capital as ``acceptance_100k`` but a
separate ledger identity, so it can never share or overwrite the legacy funded
ledger. These tests pin three things: the new scenario resolves, the two
pre-existing scenarios plus the legacy funded shape are unchanged, and every
capital/identity guard still fails closed.

Research Only / Paper Account Only / No Broker Route / Live Execution Locked.
"""

from __future__ import annotations

import unittest

from quantdesk.paper.portfolio_contracts import (
    ACCEPTANCE_CAPITAL,
    ACCEPTANCE_SCENARIO,
    DEFAULT_USER_CAPITAL,
    UNRESTRICTED_CAPITAL,
    UNRESTRICTED_SCENARIO,
    USER_SCENARIO,
    PortfolioContractError,
    resolve_paper_capital_profile,
)


class UnrestrictedScenarioTests(unittest.TestCase):
    def test_unrestricted_scenario_resolves_with_its_own_identity(self) -> None:
        profile = resolve_paper_capital_profile(
            scenario_id=UNRESTRICTED_SCENARIO,
            requested_initial_capital=UNRESTRICTED_CAPITAL,
        )

        self.assertEqual(profile.scenario_id, "unrestricted_100k")
        self.assertEqual(profile.profile_id, "unrestricted_100k")
        self.assertEqual(profile.initial_capital, 100_000.0)
        self.assertEqual(profile.source, "explicit_unrestricted_scenario")
        self.assertEqual(profile.visible_label, "Unrestricted 100k Research Paper Account")
        self.assertFalse(profile.legacy_state_preserved)
        self.assertFalse(profile.paper_credit_enabled)
        self.assertEqual(profile.credit_limit, 0.0)

    def test_unrestricted_scenario_defaults_to_its_own_capital(self) -> None:
        profile = resolve_paper_capital_profile(scenario_id=UNRESTRICTED_SCENARIO)

        self.assertEqual(profile.initial_capital, UNRESTRICTED_CAPITAL)

    def test_unrestricted_identity_is_distinct_from_acceptance(self) -> None:
        unrestricted = resolve_paper_capital_profile(scenario_id=UNRESTRICTED_SCENARIO)
        acceptance = resolve_paper_capital_profile(scenario_id=ACCEPTANCE_SCENARIO)

        self.assertEqual(unrestricted.initial_capital, acceptance.initial_capital)
        self.assertNotEqual(unrestricted.scenario_id, acceptance.scenario_id)
        self.assertNotEqual(unrestricted.visible_label, acceptance.visible_label)
        self.assertNotEqual(unrestricted.source, acceptance.source)


class PreexistingScenariosUnchangedTests(unittest.TestCase):
    def test_default_user_scenario_unchanged(self) -> None:
        profile = resolve_paper_capital_profile()

        self.assertEqual(profile.scenario_id, "user_10k")
        self.assertEqual(profile.initial_capital, DEFAULT_USER_CAPITAL)
        self.assertEqual(profile.source, "default_new_account")
        self.assertEqual(profile.visible_label, "User 10k Paper Account")

    def test_acceptance_scenario_unchanged(self) -> None:
        profile = resolve_paper_capital_profile(
            scenario_id=ACCEPTANCE_SCENARIO,
            requested_initial_capital=ACCEPTANCE_CAPITAL,
        )

        self.assertEqual(profile.scenario_id, "acceptance_100k")
        self.assertEqual(profile.initial_capital, ACCEPTANCE_CAPITAL)
        self.assertEqual(profile.source, "explicit_acceptance_scenario")
        self.assertEqual(profile.visible_label, "Acceptance 100k Paper Scenario")

    def test_legacy_funded_ledger_shape_still_resolves(self) -> None:
        state = {
            "schema_version": "3.7.0.6",
            "paper_trading_only": True,
            "initial_cash": 100_000.0,
            "profile_id": "legacy_preserved",
            "scenario_id": "legacy_existing",
            "capital_profile_label": "Legacy 100,000 Paper State (Preserved)",
        }

        profile = resolve_paper_capital_profile(
            existing_state=state,
            existing_state_verified=True,
        )

        self.assertEqual(profile.scenario_id, "legacy_existing")
        self.assertEqual(profile.profile_id, "legacy_preserved")
        self.assertEqual(profile.initial_capital, 100_000.0)
        self.assertTrue(profile.legacy_state_preserved)
        self.assertEqual(profile.source, "existing_funded_ledger")


class ScenarioGuardsStillFailClosedTests(unittest.TestCase):
    def test_unknown_scenario_is_rejected(self) -> None:
        with self.assertRaises(PortfolioContractError) as ctx:
            resolve_paper_capital_profile(scenario_id="bogus_500k")

        self.assertIn("scenario_id_not_allowed:bogus_500k", str(ctx.exception))

    def test_100k_under_user_scenario_still_names_acceptance(self) -> None:
        with self.assertRaises(PortfolioContractError) as ctx:
            resolve_paper_capital_profile(
                scenario_id=USER_SCENARIO,
                requested_initial_capital=ACCEPTANCE_CAPITAL,
            )

        self.assertIn("100000_requires_scenario_id=acceptance_100k", str(ctx.exception))

    def test_wrong_capital_for_unrestricted_is_rejected(self) -> None:
        with self.assertRaises(PortfolioContractError) as ctx:
            resolve_paper_capital_profile(
                scenario_id=UNRESTRICTED_SCENARIO,
                requested_initial_capital=50_000.0,
            )

        self.assertIn("scenario_capital_mismatch:unrestricted_100k", str(ctx.exception))

    def test_negative_capital_is_rejected(self) -> None:
        with self.assertRaises(PortfolioContractError):
            resolve_paper_capital_profile(
                scenario_id=UNRESTRICTED_SCENARIO,
                requested_initial_capital=-1.0,
            )

    def test_existing_unrestricted_state_requires_matching_capital(self) -> None:
        state = {
            "schema_version": "3.7.0.6",
            "paper_trading_only": True,
            "initial_cash": 42_000.0,
            "profile_id": "unrestricted_100k",
            "scenario_id": "unrestricted_100k",
        }

        with self.assertRaises(PortfolioContractError) as ctx:
            resolve_paper_capital_profile(existing_state=state)

        self.assertIn("existing_capital_profile_mismatch", str(ctx.exception))

    def test_existing_unrestricted_state_resolves_when_capital_matches(self) -> None:
        state = {
            "schema_version": "3.7.0.6",
            "paper_trading_only": True,
            "initial_cash": UNRESTRICTED_CAPITAL,
            "profile_id": "unrestricted_100k",
            "scenario_id": "unrestricted_100k",
        }

        profile = resolve_paper_capital_profile(existing_state=state)

        self.assertEqual(profile.scenario_id, "unrestricted_100k")
        self.assertEqual(profile.initial_capital, UNRESTRICTED_CAPITAL)


if __name__ == "__main__":
    unittest.main()
