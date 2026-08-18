# DailyReport

面向 AWS EC2 的自动化中文金融日报系统。第二阶段 mock 全流程已完成；第三阶段已加入真实 OpenAI、免费行情、Marketaux/Google News 和 FRED provider，等待有效密钥完成端到端实测。Google Drive、Gmail 和 EC2 部署仍未启用。

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
- 真实 OpenAI 与 Marketaux 尚需轮换后的有效密钥完成端到端实测。
- mock 交易日历支持周末和注入式节假日；第三阶段替换为权威交易所日历。
- 不包含真实凭据，不会发送邮件或上传 Drive。
- 第三阶段进度见 `docs/phase-3-integration.md`，成本和数据源建议见 `docs/phase-1-architecture.md`。
