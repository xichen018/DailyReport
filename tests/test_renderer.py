import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.reporting.renderer import _html_rationale, _html_report, _rationale_sections, _reader_datetime_text, _upcoming_event_entries
from app.schemas.models import Impact, InstrumentResult, InvestmentAnalysis, MarketObservation, NewsItem, PricePoint, ResearchTaskResult, RunContext, Source, TaskStatus, UpcomingEvent, Window


class RendererTests(unittest.TestCase):
    def test_reader_datetime_is_rendered_in_hong_kong_time(self) -> None:
        self.assertEqual(
            _reader_datetime_text("截至2026-08-21T04:00:00+00:00价格走弱。"),
            "截至08月21日 12:00 HKT价格走弱。",
        )

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

    def test_investment_analysis_is_rendered(self) -> None:
        now = datetime(2026, 8, 22, 8, 15, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        window = Window(timezone="Asia/Hong_Kong", start_at=now.replace(day=21), end_at=now)
        context = RunContext(run_id="test", timezone="Asia/Hong_Kong", scheduled_for=now, window=window, market_dates={})
        source = Source(source_id="src_1", provider="binance", publisher="Binance", url="https://api.binance.com", retrieved_at=now)
        result = ResearchTaskResult(
            run_id="test", request_id="req", task_id="cross_asset", title_zh="加密资产与能源",
            status=TaskStatus.SUCCESS, window=window, sources=[source],
            instruments=[InstrumentResult(
                instrument_id="bitcoin_binance", symbol="BTCUSDT", name="Bitcoin / Tether",
                asset_class="crypto", exchange="BINANCE", currency="USDT", trading_date=now.date(),
                prices=[PricePoint(kind="latest_close", value=Decimal("76683.20"), previous_value=Decimal("78034.17"), change_value=Decimal("-1350.97"), change_pct=Decimal("-1.73"), currency="USDT", as_of=now, source_ids=["src_1"])],
                news=[NewsItem(headline="现货需求变化", published_at=now, summary_zh="已确认事实。", impact=Impact.NEGATIVE, rationale_zh="该事实削弱短期需求。", source_ids=["src_1"])],
            )],
            market_observations=[MarketObservation(metric_id="btc_rsi_14d", instrument_id="bitcoin_binance", label="BTC 14日 RSI", value=Decimal("58"), unit="index", as_of=now, interpretation_zh="动量未极端。", source_ids=["src_1"])],
            investment_analyses=[InvestmentAnalysis(instrument_id="bitcoin_binance", investment_view_zh="短期更可能回撤。", key_evidence_zh=["高位放量回落支持短期回撤判断。", "长期均线仍向上，但不能证明短期继续上涨。"], key_variable_zh="若放量跌破20日均线，回撤可能扩大。", market_pricing_zh="价格正在反映风险偏好下降。", variant_view_zh="资金流尚未确认市场的一致预期。", catalysts_zh="美国数据将影响实际利率。", levels_and_actions_zh="跌破支撑后回撤风险扩大。", source_ids=["src_1"])],
            upcoming_events=[UpcomingEvent(event_at=now.replace(day=25, hour=22, minute=0), original_timezone="America/New_York", original_time_label="2026-08-25 10:00 EDT", title_zh="美国消费者信心", affected_assets_zh=["BTC", "美股"], transmission_variable_zh="美元与实际利率", why_it_matters_zh="数据将影响美元利率预期，并传导至 BTC 风险偏好。", consensus="92.0", prior="93.1", last_verified_at=now, source_ids=["src_1"])],
        )

        rendered = _html_report(context, [result], "real")

        self.assertNotIn("市场结构与资金指标", rendered)
        self.assertIn("<span>A</span>当前判断", rendered)
        self.assertIn("证据依据", rendered)
        self.assertIn("市场正在定价什么", rendered)
        self.assertIn("关键分歧与增量信息", rendered)
        self.assertIn("关键价位与应对", rendered)
        self.assertIn("<span>F</span>重要事件", rendered)
        self.assertLess(rendered.index("当前判断"), rendered.index("重要事件"))
        self.assertNotIn("观点更新", rendered)
        self.assertNotIn("<th>上一收盘</th>", rendered)
        self.assertNotIn("<th>涨跌</th>", rendered)
        self.assertNotIn("一句话判断", rendered)
        self.assertNotIn("偏强怎么看", rendered)
        self.assertNotIn("转弱怎么看", rendered)
        self.assertNotIn("裁决条件", rendered)
        self.assertNotIn("失效条件", rendered)
        self.assertNotIn("不能证明短期继续上涨", rendered)
        self.assertIn("未来一周关键事件", rendered)
        self.assertIn("美国消费者信心", rendered)
        self.assertIn("08月25日 22:00 HKT（2026-08-25 10:00 EDT）", rendered)
        self.assertIn("预期 92.0 / 前值 93.1", rendered)
        self.assertGreater(rendered.index("未来一周关键事件"), rendered.index("关键价位与应对"))

        macro_result = result.model_copy(deep=True)
        macro_result.task_id = "macro_market"
        asset_result = result.model_copy(deep=True)
        asset_result.upcoming_events[0].title_zh = "同一宏观事件的资产化改写"
        entries = _upcoming_event_entries([asset_result, macro_result])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0].task_id, "macro_market")

        asset_result.upcoming_events[0].event_at = asset_result.upcoming_events[0].event_at.replace(hour=23)
        entries = _upcoming_event_entries([asset_result, macro_result])
        self.assertEqual(len(entries), 2)


if __name__ == "__main__":
    unittest.main()
