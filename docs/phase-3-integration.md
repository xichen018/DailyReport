# 第三阶段真实数据接入记录

状态：实现完成，等待凭据兼容性和全链路真实运行验证。

## 已接入

- OpenAI Python SDK `responses.parse`，以 `ResearchTaskResult` Pydantic 模型生成严格结构化输出；不接受自由文本 fallback。
- 每个板块仍创建独立请求，不传 `previous_response_id`。
- 支持官方 OpenAI 或 OpenAI-compatible `OPENAI_BASE_URL`；兼容网关必须实际支持 Responses API 与 Structured Outputs。
- Binance BTC/USDT 公共 24 小时行情，含多个官方 REST 入口 fallback。
- Yahoo Chart 作为股票、期货和加密行情候选源；不作为唯一来源。
- Stooq 作为可用标的的日线交叉源。
- Google News RSS 与 GDELT 2.0 提供无需密钥的中英文候选新闻；Marketaux 仅作可选增强。
- FRED 公共 CSV：VIX、CPI、联邦基金有效利率基础数据。
- SEC Company Facts：为美股标的提供最近已申报季度的收入、利润、现金流、资本开支、资产和权益。程序合并同一指标的标准标签历史；对 10-Q 年初至今现金流，只在同一财政年度、同一起点且相邻期间可严格相减时还原单季值。只有期间和口径一致时才计算同比、营业利润率与自由现金流，并保留表单、申报日、accession、`period_basis` 和派生来源审计字段。
- AWS Secrets Manager JSON 与本地环境变量两种 secret 注入；所有落盘数据均不含 secret value。
- 模型结果对 provider bundle 二次校验：价格值/前值、新闻 URL、宏观 metric ID、相对指标 ID 必须来自本次原始候选数据。

## 当前验证

- 73 项自动化测试通过。
- OpenAI SDK 当前版本确认提供 `client.responses.parse(..., text_format=ResearchTaskResult)`。
- Binance 公共行情实测成功。
- 免费源 HTTP 层已加入连接重试；免费站点仍可能出现限流、地区限制或标的缺失，系统保留结构化错误。
- 尚未使用用户密钥发起请求，也未发送邮件或上传 Drive。
- 对 `https://v1a.link` 的无凭据探测显示 `/responses`、`/v1/responses`、`/v1/models` 均受认证保护（HTTP 401）；路由存在，但这不能证明支持严格 Structured Outputs。

## 凭据与网关待验证

1. 轮换已经出现在对话或附件中的旧密钥。
2. 将新值放入本地环境变量或 AWS Secrets Manager，不写入仓库文件。
3. 对配置的 `OPENAI_BASE_URL` 执行单板块 Responses API schema 测试。
4. 对 Google News RSS 与 GDELT 执行六个股票标的覆盖测试，Marketaux 可用时补充覆盖。
5. 完成一次六板块真实运行，输出各 provider 的成功率、缺失字段和调用量。

若兼容网关不支持 Responses API 的 Pydantic/JSON Schema parse，本项目会明确失败，不会切换到不可校验的 Chat Completions 文本输出。
