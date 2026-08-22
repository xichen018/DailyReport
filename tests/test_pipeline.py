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
                self.assertTrue(call["has_shared_context"])
                self.assertIn("news_categories", call["prompt"]["module"])
                self.assertIn("source_requirements", call["prompt"]["module"])
                self.assertIn("research_context", call["prompt"]["module"])
                self.assertIn("data_checks", call["prompt"]["module"])
                self.assertIn("background_search_terms", call["prompt"]["module"])
                self.assertTrue(call["prompt"]["module"]["required_research_checks"])
                self.assertIn("只能包含", call["prompt"]["common_rules"])
                self.assertIn("所有输出必须使用简体中文", call["prompt"]["common_rules"])
                self.assertIn("Yahoo Chart API 可作为股票和指数价格的主来源", call["prompt"]["common_rules"])
                self.assertIn("可验证领先信号", call["prompt"]["common_rules"])
                self.assertIn("用户提供且不可篡改的研究基线", call["prompt"]["common_rules"])
                self.assertIn("严禁修改、补写或推断", call["prompt"]["common_rules"])
                self.assertIn("审计所有量化论证", call["prompt"]["common_rules"])
                self.assertIn("区分事实、市场定价与解释", call["prompt"]["common_rules"])
                self.assertIn("是本次可使用的全部事实边界", call["prompt"]["common_rules"])
                self.assertIn("不得凭模型记忆", call["prompt"]["common_rules"])
                self.assertIn("不得补写新旧目标价", call["prompt"]["common_rules"])
                self.assertIn("`investment_analyses` 不是标的覆盖清单", call["prompt"]["common_rules"])
                self.assertIn("不得把 `shared_context` 中的指标复制", call["prompt"]["common_rules"])
                if call["task_id"] in {"hk_equities", "us_semis_optics", "us_platform_media", "cybersecurity"}:
                    self.assertIn("可直接阅读的投资含义段落", call["prompt"]["module_instructions"])
                    self.assertIn("评级和目标价调整", call["prompt"]["module_instructions"])
                    self.assertIn("合并为一条机构观点汇总", call["prompt"]["module_instructions"])
                    self.assertIn("不使用固定栏目", call["prompt"]["module_instructions"])
                    self.assertIn("不预测股价", call["prompt"]["module_instructions"])
                    self.assertNotIn("未来 2-8 周", call["prompt"]["module_instructions"])
                    self.assertNotIn("180 个汉字", call["prompt"]["module_instructions"])
                if call["task_id"] == "cross_asset":
                    self.assertIn("provider_data.market.signals", call["prompt"]["module_instructions"])
                    self.assertIn("BTC投资观点应像机构研究结论", call["prompt"]["module_instructions"])
                    self.assertIn("没有重大新闻不等于没有市场结构分析", call["prompt"]["module_instructions"])
                if call["task_id"] == "macro_market":
                    self.assertIn("固定数据检查与新闻筛选相互独立", call["prompt"]["module_instructions"])
                    self.assertIn("不得根据预定日历假设数据已经公布", call["prompt"]["module_instructions"])
                    self.assertIn("先识别市场结构，再解释原因", call["prompt"]["module_instructions"])
                if call["task_id"] == "cross_asset":
                    self.assertIn("不得因价格同时变化就认定因果", call["prompt"]["module_instructions"])
                self.assertNotIn("必须检查 Marketaux", call["prompt"]["common_rules"])
            self.assertTrue((run_dir / "merged" / "report_input.json").is_file())
            self.assertTrue((run_dir / "reports" / "daily-report.html").is_file())
            self.assertTrue((run_dir / "reports" / "daily-report.pdf").is_file())
            self.assertTrue((run_dir / "reports" / "daily-report.json").is_file())
            self.assertEqual(len(list((run_dir / "validated").glob("*.json"))), 6)
            html_report = (run_dir / "reports" / "daily-report.html").read_text(encoding="utf-8")
            self.assertIn("展开完整来源审计记录", html_report)
            self.assertIn("研究要求覆盖", html_report)
            report_json = json.loads((run_dir / "reports" / "daily-report.json").read_text(encoding="utf-8"))
            self.assertTrue(all(task["research_checks"] for task in report_json["tasks"]))
            for task in report_json["tasks"]:
                instrument_ids = {item["instrument_id"] for item in task["instruments"]}
                if instrument_ids:
                    self.assertTrue({item["instrument_id"] for item in task["investment_analyses"]} <= instrument_ids)
                    self.assertTrue(task["market_observations"])
            self.assertIn("https://example.test/market/", html_report)
            self.assertIn("Mock Exchange", html_report)
            self.assertIn("查看原文</a>", html_report)
            self.assertIn("class='instrument'", html_report)
            self.assertIn("class='instrument-index'>1.1</span>", html_report)
            self.assertIn("class='news-index'>A.1</span>", html_report)
            self.assertIn("来源：<a href=", html_report)
            self.assertNotIn("[src_1]", html_report)
            self.assertNotIn("utm_source=duplicate", html_report)

    def test_one_failure_still_generates_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self._run(Path(directory), {"cybersecurity"})
            self.assertEqual(result["status"], "partial_success")
            self.assertEqual(result["tasks"]["cybersecurity"], "failed")
            report = json.loads((Path(result["run_dir"]) / "reports" / "daily-report.json").read_text(encoding="utf-8"))
            failed = next(item for item in report["tasks"] if item["task_id"] == "cybersecurity")
            self.assertEqual(failed["errors"][0]["code"], "MOCK_TASK_FAILURE")

    def test_selected_task_runs_one_isolated_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = MockResponsesClient()
            pipeline = DailyReportPipeline(
                ROOT,
                MockProviderBundle(),
                client,
                output_root=Path(directory),
                task_ids={"hk_equities"},
            )
            result = pipeline.run(datetime(2026, 8, 18, 8, 15, 2, tzinfo=ZoneInfo("Asia/Hong_Kong")))
            self.assertEqual(result["tasks"], {"hk_equities": "success"})
            self.assertEqual(len(client.calls), 1)

    def test_validation_retry_receives_specific_feedback(self) -> None:
        class RetryClient(MockResponsesClient):
            def create(self, module, prompt, provider_data):
                raw = super().create(module, prompt, provider_data)
                if len(self.calls) == 1:
                    raw["task_id"] = "wrong_task"
                return raw

        with tempfile.TemporaryDirectory() as directory:
            client = RetryClient()
            pipeline = DailyReportPipeline(
                ROOT,
                MockProviderBundle(),
                client,
                output_root=Path(directory),
                task_ids={"hk_equities"},
            )
            result = pipeline.run(datetime(2026, 8, 18, 8, 15, 3, tzinfo=ZoneInfo("Asia/Hong_Kong")))

            self.assertEqual(result["tasks"], {"hk_equities": "success"})
            self.assertEqual(len(client.calls), 2)
            feedback = client.calls[1]["prompt"]["validation_feedback"]
            self.assertIn("task_id does not match module", feedback)
            self.assertIn("不得只返回补丁", feedback)


if __name__ == "__main__":
    unittest.main()
