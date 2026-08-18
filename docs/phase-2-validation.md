# 第二阶段验收记录

状态：完成  
验收日期：2026-08-18  
模式：全链路 mock，不调用外部 API

## 已实现

- 6 个 TOML 板块配置与独立任务输入。
- 公共规则、板块模板和动态 `RunContext` 分离。
- 每个板块独立 request ID，不使用 `previous_response_id`，不读取其他板块结果。
- Pydantic 严格统一 schema，拒绝额外字段和无法解析的自由文本。
- provider 协议：行情、新闻、宏观、交易日历均可替换。
- HKT 动态调研窗口；mock 日历支持周末和注入式节假日。
- 标的代码、价格变化、新闻窗口、URL、来源引用、相对比率和重复新闻校验。
- 单任务失败隔离；其他任务继续执行并生成带失败原因的报告。
- raw provider、raw OpenAI、validated、merged、HTML、PDF、JSON、manifest 和 JSONL 日志落盘。
- CLI：`run`、`healthcheck`、`schema`。

## 自动化测试

执行命令：

```bash
python -m unittest discover -s tests -v
```

结果：8 项测试全部通过。

覆盖范围：

- 动态 30 小时窗口；
- 周末与注入式节假日；
- 6 个板块配置及关键代码；
- 价格与涨跌幅重算；
- URL 规范化与重复新闻删除；
- 新闻窗口约束；
- 6 个独立请求和 request ID；
- 全部产物生成；
- `cybersecurity` 模拟失败后的 `partial_success` 报告。

## 完整运行

最终成功运行：`20260818T081506+0800`

```text
data/runs/20260818T081506+0800/
├── raw/providers/<task_id>/bundle.json
├── raw/openai/<task_id>.json
├── validated/<task_id>.json
├── merged/report_input.json
├── reports/daily-report.html
├── reports/daily-report.pdf
├── reports/daily-report.json
├── logs/run.jsonl
├── run_context.json
└── run_manifest.json
```

6 个板块状态均为 `success`。另在临时目录完成故障注入运行，`cybersecurity=failed`，运行级状态为 `partial_success`，HTML/PDF/JSON 报告仍成功生成且包含失败原因。

## PDF 质量检查

- 使用嵌入式 CJK TrueType 字体，避免依赖 PDF 阅读器的 Adobe-GB1 字体包。
- 使用 Poppler 将最终 PDF 渲染为 3 页 PNG 并逐页检查。
- 中文、英文、数字和表格均可读；未发现缺字、重叠、截断或标题与表格跨页分离。

## 第三阶段前的已知边界

- mock 日历不是权威交易所节假日日历，真实阶段必须替换。
- mock provider 只验证接口和数据流，不代表真实数据覆盖。
- Linux Docker/EC2 需安装 `fonts-noto-cjk`，PDF 生成器已提供该字体路径 fallback。
- 尚未实现真实 OpenAI、Secrets Manager、行情、新闻、宏观、Drive、Gmail、Docker 或 systemd；这些分别属于第三至第五阶段。

