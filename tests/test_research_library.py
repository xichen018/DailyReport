from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.research.library import ResearchLibrary, import_material


class ResearchLibraryTests(unittest.TestCase):
    def test_import_is_versioned_and_retrieved_by_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "btc.md"
            material.write_text("BTC ETF资金流是短期需求的重要变量。若实际利率上升，估值压力可能增加。", encoding="utf-8")
            record = import_material(
                str(material), root / "library", source_name="用户材料", assets=["BTC"],
                published_on=date.today(), horizon="tactical",
            )
            matches = ResearchLibrary(root / "library").relevant({"BTC"}, date.today())
            self.assertEqual(matches[0].record_id, record.record_id)
            self.assertEqual(len(list((root / "library").glob("*.json"))), 1)

    def test_expired_material_is_not_injected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "old.txt"
            material.write_text("黄金实际利率框架是主要分析变量。", encoding="utf-8")
            import_material(
                str(material), root / "library", source_name="旧材料", assets=["黄金"],
                published_on=date(2020, 1, 1), horizon="tactical",
            )
            self.assertEqual(ResearchLibrary(root / "library").relevant({"黄金"}, date.today()), [])


if __name__ == "__main__":
    unittest.main()
