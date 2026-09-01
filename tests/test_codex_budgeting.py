import unittest

from scripts.codex_budgeting import (
    eligible_codex_accounts,
    evaluate_account_budget,
    normalize_codex_budget_settings,
)


class CodexBudgetingTests(unittest.TestCase):
    def test_default_budget_blocks_account_below_hourly_floor(self):
        settings = {
            "codex_default_budget": {
                "hourly_pct_min": 50,
                "weekly_pct_min": 0,
                "monthly_credits_min": 0,
            },
            "codex_account_budgets": {},
        }
        account = {
            "email": "a@example.com",
            "status": "active",
            "usage": {"secondaryRemainingPercent": 20, "primaryRemainingPercent": 90},
            "remainingCreditsSecondary": 100,
        }
        evaluation = evaluate_account_budget(settings, account)
        self.assertFalse(evaluation["ok"])
        self.assertTrue(any("hourly" in reason for reason in evaluation["reasons"]))

    def test_per_account_override_can_tighten_weekly_and_monthly(self):
        settings = {
            "codex_default_budget": {
                "hourly_pct_min": 10,
                "weekly_pct_min": 0,
                "monthly_credits_min": 0,
            },
            "codex_account_budgets": {
                "a@example.com": {
                    "hourly_pct_min": 10,
                    "weekly_pct_min": 60,
                    "monthly_credits_min": 200,
                }
            },
        }
        account = {
            "email": "a@example.com",
            "status": "active",
            "usage": {"secondaryRemainingPercent": 80, "primaryRemainingPercent": 40},
            "remainingCreditsSecondary": 100,
        }
        evaluation = evaluate_account_budget(settings, account)
        self.assertFalse(evaluation["ok"])
        self.assertTrue(any("weekly" in reason for reason in evaluation["reasons"]))
        self.assertTrue(any("monthly" in reason for reason in evaluation["reasons"]))

    def test_eligible_accounts_filters_to_accounts_that_pass(self):
        settings = {}
        normalize_codex_budget_settings(settings)
        settings["codex_default_budget"]["hourly_pct_min"] = 50
        accounts = [
            {
                "email": "good@example.com",
                "status": "active",
                "usage": {"secondaryRemainingPercent": 75, "primaryRemainingPercent": 75},
                "remainingCreditsSecondary": 100,
            },
            {
                "email": "bad@example.com",
                "status": "active",
                "usage": {"secondaryRemainingPercent": 20, "primaryRemainingPercent": 90},
                "remainingCreditsSecondary": 100,
            },
        ]
        eligible = eligible_codex_accounts(settings, accounts)
        self.assertEqual([a["email"] for a in eligible], ["good@example.com"])


if __name__ == "__main__":
    unittest.main()
