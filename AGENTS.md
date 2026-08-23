# DailyReport Working Context

## Product Direction

- Produce a professional Chinese investment-research daily report for AWS EC2 production.
- Analysis quality is the priority: use asset-specific structured data, causal transmission, competing scenarios, decision points, invalidation conditions, and explicit evidence gaps.
- Reuse the common evidence-to-scenario framework across assets, but never copy BTC-specific ETF, on-chain, or derivatives logic into equities or oil.
- Preserve user-provided research and investment context exactly. Never fill factual gaps from model memory.

## Report Language

- The reader-facing report must use investment-research language, not pipeline or engineering language.
- Do not show `failed`, `partial success`, stack traces, provider exceptions, or delivery status in the report body.
- Missing evidence should appear as `待补数据` or a concise statement that available evidence is insufficient for a reliable conclusion.
- Keep isolated indicator readings in structured audit data. Do not promote a metric to reader-facing evidence unless comparison, abnormality, direction, or corroboration makes it decision-useful; omit boilerplate that only says the metric cannot determine direction.
- Keep detailed failures and diagnostics in JSON manifests and logs for operators.
- Preview reports must be clearly labeled as design/process previews and must not look like real investment conclusions.

## Delivery

- Production runs from `/home/ubuntu/DailyReport` using the checked-in systemd service and timer.
- Secrets stay in AWS Secrets Manager or the server environment file. Never commit or print secret values.
- Before deploying, run the full test suite, commit scoped source changes, push the commit, update EC2, run the service, and visually inspect the generated PDF.
- Do not stage the reference Word document or generated `data/` artifacts unless the user explicitly requests it.

## Continuity

- Read this file, recent Git commits, and `docs/phase-3-integration.md` at the start of a new task.
- Record durable product decisions here or in `docs/` so a new Codex task can recover context without relying on prior chat history.
- The versioned research memory lives in `research/library/`. User-provided materials are imported with `daily-report import-research`; active records are retrieved by asset/topic on every run.
- Research memory is opinion context, not a current-fact store. Preserve author/date/provenance, retain conflicts, and exclude expired records from prompts.
- Upcoming events shown to readers must be single, source-backed events with a concrete date. Weekly date ranges and relative dates are discovery material only and must not appear as calendar entries.
- Reader-facing event times use Hong Kong time and retain the original timezone label. Only confirmed events appear in the PDF calendar.
