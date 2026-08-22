import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.reporting.renderer import _html_rationale, _html_report, _rationale_sections
from app.schemas.models import MarketObservation, ResearchTaskResult, RunContext, ScenarioAnalysis, Source, TaskStatus, Window


class RendererTests(unittest.TestCase):
    def test_structured_rationale_is_split_into_scanable_sections(self) -> None:
        value = (
            "影响判断：【中性】核心变化：增量有限。"
            "传导分析：验证需求。关键验证：正向证据：订单；负向证据：取消。"
        )

        sections = _rationale_sections(value)

        self.assertEqual([label for label, _ in sections], ["影响判断：", "核心变化：", "传导分析：", "关键验证："])
        rendered = _html_rationale(value)
        self.assertIn("rationale-structured", rendered)
        self.assertEqual(rendered.count("<p>"), 4)

    def test_unstructured_rationale_keeps_legacy_investment_label(self) -> None:
        rendered = _html_rationale("库存增加与供应风险形成相反影响。")

        self.assertIn("<strong>投资含义：</strong>", rendered)
        self.assertNotIn("rationale-structured", rendered)

    def test_market_structure_and_scenario_are_rendered(self) -> None:
        now = datetime(2026, 8, 22, 8, 15, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        window = Window(timezone="Asia/Hong_Kong", start_at=now.replace(day=21), end_at=now)
        context = RunContext(run_id="test", timezone="Asia/Hong_Kong", scheduled_for=now, window=window, market_dates={})
        source = Source(source_id="src_1", provider="binance", publisher="Binance", url="https://api.binance.com", retrieved_at=now)
        result = ResearchTaskResult(
            run_id="test", request_id="req", task_id="cross_asset", title_zh="加密资产与能源",
            status=TaskStatus.SUCCESS, window=window, sources=[source],
            market_observations=[MarketObservation(metric_id="btc_rsi_14d", instrument_id="bitcoin_binance", label="BTC 14日 RSI", value=Decimal("58"), unit="index", as_of=now, interpretation_zh="动量未极端。", source_ids=["src_1"])],
            scenario_analyses=[ScenarioAnalysis(instrument_id="bitcoin_binance", current_regime_zh="区间。", base_case_zh="维持。", alternative_case_zh="破位。", decision_points_zh="观察RSI。", invalidation_zh="结构反转。", evidence_limits_zh="缺ETF数据。", source_ids=["src_1"])],
        )

        rendered = _html_report(context, [result], "real")

        self.assertIn("市场结构与资金指标", rendered)
        self.assertIn("情景研判", rendered)
        self.assertIn("缺ETF数据", rendered)
        self.assertIn("bitcoin_binance 情景研判", rendered)


if __name__ == "__main__":
    unittest.main()
