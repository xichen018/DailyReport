from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.integrations.ses_email import SesDeliveryError, send_pdf_report


class FakeSesClient:
    def __init__(self) -> None:
        self.calls = []

    def send_raw_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "ses-test-message"}


class SesEmailTests(unittest.TestCase):
    def test_sends_pdf_and_writes_idempotent_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            pdf = run_dir / "reports" / "daily-report.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.4 test")
            result = {
                "run_dir": str(run_dir),
                "run_id": "run-1",
                "status": "partial_success",
                "tasks": {"one": "success", "two": "failed"},
                "reports": {"pdf": str(pdf)},
            }
            client = FakeSesClient()

            first = send_pdf_report(
                result,
                sender="sender@example.com",
                recipients=("recipient@example.com",),
                region="ap-southeast-1",
                ses_client=client,
            )
            second = send_pdf_report(
                result,
                sender="sender@example.com",
                recipients=("recipient@example.com",),
                region="ap-southeast-1",
                ses_client=client,
            )

            self.assertEqual(first["status"], "sent")
            self.assertEqual(second["message_id"], "ses-test-message")
            self.assertEqual(len(client.calls), 1)
            raw_message = client.calls[0]["RawMessage"]["Data"]
            self.assertIn(b"application/pdf", raw_message)
            record = json.loads((run_dir / "delivery" / "ses.json").read_text(encoding="utf-8"))
            self.assertEqual(record["recipients"], ["recipient@example.com"])

    def test_requires_existing_pdf(self) -> None:
        with self.assertRaises(SesDeliveryError):
            send_pdf_report(
                {"run_dir": "/tmp/missing", "run_id": "x", "status": "failed", "tasks": {}, "reports": {"pdf": "/tmp/missing.pdf"}},
                sender="sender@example.com",
                recipients=("recipient@example.com",),
                region="ap-southeast-1",
                ses_client=FakeSesClient(),
            )


if __name__ == "__main__":
    unittest.main()
