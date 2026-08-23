import unittest

from app.research.sources import source_policy


class SourcePolicyTests(unittest.TestCase):
    def test_paid_media_snippet_cannot_be_treated_as_full_text(self) -> None:
        policy = source_policy("https://www.ft.com/content/example", "Financial Times")
        self.assertEqual(policy.tier, "professional_media")
        self.assertEqual(policy.content_access, "snippet_only")

    def test_official_source_can_confirm_fact(self) -> None:
        policy = source_policy("https://www.bls.gov/schedule/news_release/cpi.htm", "BLS")
        self.assertEqual(policy.tier, "primary")
        self.assertEqual(policy.role, "confirmed_fact")


if __name__ == "__main__":
    unittest.main()
