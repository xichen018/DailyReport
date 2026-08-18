from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.integrations.mock_openai import MockResponsesClient
from app.orchestrator.pipeline import DailyReportPipeline
from app.providers.mock import MockProviderBundle


ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def _run(self, output: Path, failures: set[str] | None = None) -> tuple[dict, MockResponsesClient]:
        client = MockResponsesClient(failures)
        pipeline = DailyReportPipeline(ROOT, MockProviderBundle(), client, output_root=output)
        result = pipeline.run(datetime(2026, 8, 18, 8, 15, 1, tzinfo=ZoneInfo("Asia/Hong_Kong")))
        return result, client

    def test_complete_run_writes_all_artifacts_and_isolated_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, client = self._run(Path(directory))
            run_dir = Path(result["run_dir"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(len(client.calls), 6)
            self.assertEqual(len({call["request_id"] for call in client.calls}), 6)
            for call in client.calls:
                self.assertEqual(call["task_id"], call["prompt"]["module"]["task_id"])
                self.assertNotIn("previous_response_id", call["prompt"])
                self.assertIn("news_categories", call["prompt"]["module"])
                self.assertIn("source_requirements", call["prompt"]["module"])
            self.assertTrue((run_dir / "merged" / "report_input.json").is_file())
            self.assertTrue((run_dir / "reports" / "daily-report.html").is_file())
            self.assertTrue((run_dir / "reports" / "daily-report.pdf").is_file())
            self.assertTrue((run_dir / "reports" / "daily-report.json").is_file())
            self.assertEqual(len(list((run_dir / "validated").glob("*.json"))), 6)
            html_report = (run_dir / "reports" / "daily-report.html").read_text(encoding="utf-8")
            self.assertIn("来源与时间戳", html_report)
            self.assertIn("研究要求覆盖", html_report)
            report_json = json.loads((run_dir / "reports" / "daily-report.json").read_text(encoding="utf-8"))
            self.assertTrue(all(task["research_checks"] for task in report_json["tasks"]))
            self.assertIn("https://example.test/market/", html_report)
            self.assertIn("[src_1]", html_report)
            self.assertNotIn("utm_source=duplicate", html_report)

    def test_one_failure_still_generates_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self._run(Path(directory), {"cybersecurity"})
            self.assertEqual(result["status"], "partial_success")
            self.assertEqual(result["tasks"]["cybersecurity"], "failed")
            report = json.loads((Path(result["run_dir"]) / "reports" / "daily-report.json").read_text(encoding="utf-8"))
            failed = next(item for item in report["tasks"] if item["task_id"] == "cybersecurity")
            self.assertEqual(failed["errors"][0]["code"], "MOCK_TASK_FAILURE")


if __name__ == "__main__":
    unittest.main()
