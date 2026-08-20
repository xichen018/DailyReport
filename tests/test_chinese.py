from __future__ import annotations

import unittest

from app.text.chinese import simplify_strings, to_simplified_chinese


class ChineseNormalizationTests(unittest.TestCase):
    def test_converts_traditional_chinese_to_simplified(self) -> None:
        self.assertEqual(to_simplified_chinese("財經新聞與騰訊行情"), "财经新闻与腾讯行情")

    def test_recursive_conversion_preserves_urls(self) -> None:
        value = {
            "headline": "財報與業績指引",
            "sources": [{"publisher": "財經新聞", "url": "https://example.test/財經新聞"}],
        }

        normalized = simplify_strings(value)

        self.assertEqual(normalized["headline"], "财报与业绩指引")
        self.assertEqual(normalized["sources"][0]["publisher"], "财经新闻")
        self.assertEqual(normalized["sources"][0]["url"], value["sources"][0]["url"])


if __name__ == "__main__":
    unittest.main()
