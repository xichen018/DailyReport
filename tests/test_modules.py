from __future__ import annotations

import unittest
from pathlib import Path

from app.modules.loader import load_module_configs


ROOT = Path(__file__).resolve().parents[1]


class ModuleConfigTests(unittest.TestCase):
    def test_six_isolated_modules_load(self) -> None:
        modules = load_module_configs(ROOT / "app" / "modules")
        self.assertEqual(len(modules), 6)
        self.assertEqual(len({module.task_id for module in modules}), 6)
        by_id = {module.task_id: module for module in modules}
        self.assertEqual([item.symbol for item in by_id["hk_equities"].instruments], ["1772.HK", "6166.HK"])
        self.assertEqual([item.symbol for item in by_id["cross_asset"].instruments], ["BTCUSDT", "CL1"])

    def test_attachment_news_categories_are_explicit_and_complete(self) -> None:
        by_id = {item.task_id: item for item in load_module_configs(ROOT / "app" / "modules")}
        mandatory = {
            "财报/业绩指引",
            "分析师评级或目标价变动",
            "产品与技术发布",
            "大额订单/合作",
            "管理层变动",
            "监管法律动态",
        }
        for task_id in ("hk_equities", "us_semis_optics", "us_platform_media", "cybersecurity"):
            self.assertTrue(mandatory.issubset(set(by_id[task_id].news_categories)), task_id)

    def test_asset_specific_focus_from_attachment_is_preserved(self) -> None:
        by_id = {item.task_id: item for item in load_module_configs(ROOT / "app" / "modules")}
        focus = {
            instrument.symbol: set(instrument.focus)
            for module in by_id.values()
            for instrument in module.instruments
        }
        expected = {
            "1772.HK": {"锂价", "锂电产业链"},
            "6166.HK": {"光模块", "AI算力需求"},
            "MU": {"HBM", "DRAM/NAND定价", "存储周期"},
            "COHR": {"光模块", "数据中心光互连"},
            "GOOG": {"Gemini", "AI搜索", "Google Cloud", "TPU", "反垄断案件进展"},
            "DJT": {"比特币储备与加密资产持仓", "Truth Social动态", "股份解禁", "增发"},
            "CRWD": {"Falcon平台", "Charlotte AI", "PANW/S/ZS竞争动态", "行业并购"},
            "BTCUSDT": {"ETF资金流向", "SEC与立法", "宏观流动性", "链上异动", "衍生品市场异动"},
            "CL1": {"OPEC+", "EIA/API库存与预期", "IEA/EIA/OPEC需求预测", "美元走势影响"},
        }
        for symbol, required in expected.items():
            self.assertTrue(required.issubset(focus[symbol]), symbol)

    def test_execution_and_source_policies_are_configured(self) -> None:
        by_id = {item.task_id: item for item in load_module_configs(ROOT / "app" / "modules")}
        for module in by_id.values():
            self.assertTrue(module.price_checks, module.task_id)
            self.assertTrue(module.source_requirements, module.task_id)
            self.assertTrue(module.search_terms_zh, module.task_id)
            self.assertTrue(module.search_terms_en, module.task_id)
            self.assertIn("无重大新闻", module.no_news_policy)
        self.assertTrue(any("超过1%" in item for item in by_id["us_semis_optics"].triggered_checks))
        self.assertTrue(any("明显下跌" in item for item in by_id["us_platform_media"].triggered_checks))
        macro = by_id["macro_market"]
        self.assertEqual(len(macro.data_checks), 9)
        self.assertTrue(any("CME FedWatch" in item for item in macro.data_checks))
        self.assertTrue(any("S&P 500" in item and "NTM PE" in item for item in macro.data_checks))
        self.assertTrue(any("SOX" in item and "PB" in item for item in macro.data_checks))
        self.assertTrue(any("CNN Fear & Greed" in item for item in macro.data_checks))
        self.assertTrue(any("AAII" in item for item in macro.data_checks))
        self.assertTrue(macro.background_search_terms_zh)
        self.assertTrue(macro.background_search_terms_en)


if __name__ == "__main__":
    unittest.main()
