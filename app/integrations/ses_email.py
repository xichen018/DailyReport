from __future__ import annotations

import json
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


HKT = ZoneInfo("Asia/Hong_Kong")


class SesDeliveryError(RuntimeError):
    pass


def _write_delivery_record(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def send_pdf_report(
    run_result: dict[str, Any],
    *,
    sender: str,
    recipients: tuple[str, ...],
    region: str,
    ses_client: Any | None = None,
) -> dict[str, Any]:
    if not sender or not recipients:
        raise SesDeliveryError("SES_SENDER and SES_RECIPIENTS are required for delivery")

    run_dir = Path(str(run_result["run_dir"]))
    pdf_path = Path(str(run_result["reports"]["pdf"]))
    if not pdf_path.is_file():
        raise SesDeliveryError(f"PDF report does not exist: {pdf_path}")

    record_path = run_dir / "delivery" / "ses.json"
    if record_path.is_file():
        existing = json.loads(record_path.read_text(encoding="utf-8"))
        if existing.get("status") == "sent":
            return existing

    now = datetime.now(HKT)
    task_states = run_result.get("tasks", {})
    delivered = sum(state != "failed" for state in task_states.values())
    subject = f"金融日报｜{now:%Y-%m-%d}｜{run_result['status']}"
    body = (
        "您好，\n\n"
        f"{now:%Y-%m-%d} 自动化金融日报见附件 PDF。\n"
        f"本次共运行 {len(task_states)} 个研究板块，已交付 {delivered} 个；"
        "失败或数据限制已在报告中逐项标明。\n\n"
        "本邮件由 DailyReport 在 AWS EC2 自动生成并通过 Amazon SES 发送。"
    )

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)
    message.add_attachment(pdf_path.read_bytes(), maintype="application", subtype="pdf", filename="daily-report.pdf")

    client = ses_client
    if client is None:
        import boto3

        client = boto3.client("ses", region_name=region)

    try:
        response = client.send_raw_email(
            Source=sender,
            Destinations=list(recipients),
            RawMessage={"Data": message.as_bytes()},
        )
    except Exception as exc:
        failed_record = {
            "status": "failed",
            "run_id": run_result["run_id"],
            "attempted_at": now.isoformat(),
            "sender": sender,
            "recipients": list(recipients),
            "attachment": str(pdf_path),
            "error": str(exc),
        }
        _write_delivery_record(record_path, failed_record)
        raise SesDeliveryError(f"SES delivery failed: {exc}") from exc

    sent_record = {
        "status": "sent",
        "run_id": run_result["run_id"],
        "sent_at": datetime.now(HKT).isoformat(),
        "sender": sender,
        "recipients": list(recipients),
        "attachment": str(pdf_path),
        "message_id": response["MessageId"],
    }
    _write_delivery_record(record_path, sent_record)
    return sent_record
