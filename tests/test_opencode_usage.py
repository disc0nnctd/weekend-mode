import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.opencode_usage import summarize_opencode_usage


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class OpenCodeUsageTests(unittest.TestCase):
    def _make_db(self) -> Path:
        tmpdir = Path(tempfile.mkdtemp())
        db_path = tmpdir / "opencode.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                model TEXT,
                cost REAL DEFAULT 0 NOT NULL,
                tokens_input INTEGER DEFAULT 0 NOT NULL,
                tokens_output INTEGER DEFAULT 0 NOT NULL,
                tokens_reasoning INTEGER DEFAULT 0 NOT NULL,
                tokens_cache_read INTEGER DEFAULT 0 NOT NULL,
                tokens_cache_write INTEGER DEFAULT 0 NOT NULL,
                time_updated INTEGER
            )
            """
        )
        conn.commit()
        conn.close()
        return db_path

    def _insert_session(
        self,
        db_path: Path,
        *,
        session_id: str,
        provider_id: str,
        model_id: str,
        cost_usd: float,
        updated_at: datetime,
        tokens_input: int = 0,
        tokens_output: int = 0,
    ) -> None:
        payload = json.dumps({"id": model_id, "providerID": provider_id, "variant": "default"})
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO session (
                id, model, cost, tokens_input, tokens_output,
                tokens_reasoning, tokens_cache_read, tokens_cache_write, time_updated
            ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?)
            """,
            (session_id, payload, cost_usd, tokens_input, tokens_output, _to_ms(updated_at)),
        )
        conn.commit()
        conn.close()

    def test_summarize_opencode_usage_uses_rolling_and_month_windows(self):
        db_path = self._make_db()
        now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
        self._insert_session(
            db_path,
            session_id="a",
            provider_id="opencode",
            model_id="deepseek-v4-pro",
            cost_usd=5.0,
            updated_at=now - timedelta(hours=1),
            tokens_input=100,
            tokens_output=50,
        )
        self._insert_session(
            db_path,
            session_id="b",
            provider_id="opencode",
            model_id="deepseek-v4-flash",
            cost_usd=10.0,
            updated_at=now - timedelta(days=2),
            tokens_input=200,
            tokens_output=80,
        )
        self._insert_session(
            db_path,
            session_id="c",
            provider_id="openai",
            model_id="gpt-5",
            cost_usd=99.0,
            updated_at=now - timedelta(hours=1),
        )

        summary = summarize_opencode_usage(db_path=db_path, now=now)

        self.assertIsNotNone(summary)
        self.assertEqual(summary["session_count"], 2)
        self.assertAlmostEqual(summary["windows"]["5h"]["cost_usd"], 5.0)
        self.assertAlmostEqual(summary["windows"]["7d"]["cost_usd"], 15.0)
        self.assertAlmostEqual(summary["windows"]["month"]["cost_usd"], 15.0)
        self.assertEqual(summary["models"][0]["model_id"], "deepseek-v4-flash")

    def test_returns_none_when_no_opencode_sessions_exist(self):
        db_path = self._make_db()
        now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
        self._insert_session(
            db_path,
            session_id="only-openai",
            provider_id="openai",
            model_id="gpt-5",
            cost_usd=2.0,
            updated_at=now - timedelta(minutes=5),
        )

        summary = summarize_opencode_usage(db_path=db_path, now=now)
        self.assertIsNone(summary)


if __name__ == "__main__":
    unittest.main()
