import unittest

from scripts.reset_detection import detect_resets, is_real_reset


class ResetDetectionTests(unittest.TestCase):
    def test_rejects_timestamp_change_without_usage_drop(self):
        self.assertFalse(
            is_real_reset(
                "2026-06-22T00:00:00+00:00",
                "2026-06-22T05:00:00+00:00",
                100,
                100,
                min_drop_pct=10,
                min_window_shift_sec=3 * 3600,
            )
        )

    def test_rejects_weekly_shift_when_usage_still_high(self):
        events = detect_resets(
            {
                "claude_7d_pct": 92,
                "claude_7d_resets_at": "2026-06-22T00:00:00+00:00",
                "codex_accounts": [],
            },
            {
                "5h_pct": 0,
                "5h_resets_at": "2026-06-22T00:00:00+00:00",
                "7d_pct": 48,
                "7d_resets_at": "2026-06-29T00:00:00+00:00",
            },
            [],
        )
        self.assertFalse(any("weekly" in event.lower() for event in events))

    def test_accepts_real_5h_reset(self):
        events = detect_resets(
            {
                "claude_5h_pct": 88,
                "claude_5h_resets_at": "2026-06-22T00:00:00+00:00",
                "codex_accounts": [],
            },
            {
                "5h_pct": 12,
                "5h_resets_at": "2026-06-22T05:00:00+00:00",
                "7d_pct": 60,
                "7d_resets_at": "2026-06-29T00:00:00+00:00",
            },
            [],
        )
        self.assertTrue(any("5h window reset" in event for event in events))


if __name__ == "__main__":
    unittest.main()
