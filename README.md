# DailyReport

面向 AWS EC2 的自动化中文金融日报系统。mock 与真实 OpenAI 全流程已完成；新闻使用无需密钥的 Google News RSS 与 GDELT 2.0，Marketaux 仅作可选增强，宏观数据使用 FRED。日报 PDF 可通过 Amazon SES 自动投递。

## 研究材料记忆

用户提供的 PDF、DOCX、Markdown、文本或合法公开 URL 可导入版本化研究库：

```bash
daily-report import-research ./analysis.pdf --source "研究机构" --author "作者" --asset BTC --topic ETF --horizon structural
```

记录保存于 `research/library/`，包含来源、适用资产、期限、观点摘录、有效期与复核日。材料观点不会被当作当前事实；过期记录不会进入日报 prompt，冲突观点不会互相覆盖。不要将受版权保护的媒体全文或凭据提交到仓库。

## 本地运行

要求 Python 3.12+。正式环境安装：

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

当前 Codex 工作区可直接使用已捆绑依赖的 Python：

```bash
PYTHON=/Users/lxc_mac_pro/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
$PYTHON -m app.cli healthcheck
$PYTHON -m app.cli run
```

固定时间重放或注入失败：

```bash
$PYTHON -m app.cli run --as-of 2026-08-18T08:15:00+08:00
$PYTHON -m app.cli run --fail-task cybersecurity
```

真实模式只从环境变量或 AWS Secrets Manager 读取密钥：

```bash
export DAILY_REPORT_MODE=real
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_API_KEY='...'
export MARKETAUX_API_TOKEN='...'
python3 -m app.cli healthcheck --mode real
python3 -m app.cli run --mode real
```

生成后通过 Amazon SES 发送 PDF：

```bash
export SES_SENDER=sender@example.com
export SES_RECIPIENTS=recipient@example.com
python3 -m app.cli run --mode real --deliver
```

EC2 使用 `deploy/systemd/daily-report.service` 和 `daily-report.timer`，每天按 `Asia/Hong_Kong` 时区 08:15 运行，包括周末和节假日。

使用 OpenAI-compatible 网关时，把 `OPENAI_BASE_URL` 设置为供应商给出的完整 API base URL。该网关必须实现 Responses API 的 `responses.parse`/JSON Schema 语义；只兼容 Chat Completions 的网关不能用于本项目，因为系统禁止自由文本降级。

EC2 生产环境设置 `DAILY_REPORT_SECRET_ID`，对应 Secrets Manager JSON 仅包含 `openai_api_key` 和 `marketaux_api_token` 等 secret value。不要同时在环境变量中保存值。

运行产物位于 `data/runs/<run_id>/`。生产阶段接入 Drive 后，只有上传并校验成功才清理本地暂存目录。

## 测试

```bash
$PYTHON -m unittest discover -s tests -v
```

测试覆盖动态窗口和交易日、6 个配置加载、价格与比率重算、URL 规范化、重复新闻、窗口约束、独立请求、完整产物和单板块失败隔离。

## 当前边界

- `mock` 为默认模式，必须显式指定 `--mode real` 才调用真实服务。
- 真实免费行情目前使用 Binance、Yahoo Chart 辅助端点和 Stooq 交叉源；免费端点可能限流或覆盖不足，缺失必须进入结构化数据质量记录。
- Marketaux Token 非必需；注册或额度异常不会阻断日报。
- mock 交易日历支持周末和注入式节假日；第三阶段替换为权威交易所日历。
- 不包含真实凭据；只有显式传入 `--deliver` 才会通过 SES 发送邮件。
- 第三阶段进度见 `docs/phase-3-integration.md`，成本和数据源建议见 `docs/phase-1-architecture.md`。
