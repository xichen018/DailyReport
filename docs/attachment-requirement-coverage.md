# 附件研究要求覆盖矩阵

本文件将 `8_12 daily r.docx` 的长期研究要求映射到参数化配置。附件中的示例日期、截图价格、明文 API token 和示例 URL 查询参数不进入配置；日期和窗口由 `RunContext` 动态生成，凭据由生产环境 Secrets Manager 注入。

## 公共规则

| 附件要求 | 实现位置 |
|---|---|
| 严禁编造 | `app/prompts/common_rules.md` |
| 每项事实注明媒体/机构与 URL | 各 TOML `source_requirements` +统一 schema `source_ids` |
| 窗口内无重大新闻明确写“无重大新闻” | 各 TOML `no_news_policy` + `no_major_news` schema |
| 窗口外近期背景标日期 | 各 TOML `background_policy` + `outside_window` |
| 中英文检索 | 各 TOML `search_terms_zh` / `search_terms_en` |
| 股票任务必须查 Marketaux | 股票 TOML `source_requirements` |
| 动态日期、30 小时窗口、最近/上一交易日 | `RunContext` + 各 TOML `price_checks` |
| 新闻影响【利好】【利空】【中性】及 1-2 句逻辑 | `NewsItem.impact` / `rationale_zh` +板块模板 |

## 板块要求

| 板块 | 原文必查内容 | 配置映射 |
|---|---|---|
| 港股 | 收盘价/涨跌幅；财报指引、评级目标价、产品技术、大额订单合作、管理层、监管法律 | `hk_equities.toml` 的 `price_checks` / `news_categories` |
| 港股行业 | 赣锋：锂价、锂电产业链；剑桥：光模块、AI 算力需求 | 对应 instrument `focus` + `industry_topics` |
| MU/COHR | 最近及上一收盘；盘前盘后 >1%；六类公司新闻 | `us_semis_optics.toml` 的 `price_checks` / `triggered_checks` / `news_categories` |
| MU/COHR 行业 | HBM、DRAM/NAND、存储周期、光模块、数据中心光互连、AI 资本开支/数据中心建设 | 对应 instrument `focus` + `industry_topics` |
| GOOG/DJT | 最近及上一收盘；盘前盘后 >1%；六类公司新闻；GOOG 明显下跌原因 | `us_platform_media.toml` 的 `price_checks` / `news_categories` / `triggered_checks` |
| GOOG | Gemini、AI 搜索、Cloud、TPU、反垄断 | GOOG `focus` |
| DJT | 比特币储备、Truth Social、政治消息、解禁、增发 | DJT `focus` |
| BTC | Binance BTC/USDT 最新价、24h/30h；ETF、监管、机构交易、流动性、链上/衍生品、波动原因 | `cross_asset.toml` 的 `price_checks` / BTC `focus` |
| WTI | NYMEX CL 近月官方结算/涨跌；OPEC+、地缘、库存、预测、美元 | `cross_asset.toml` 的 `price_checks` / WTI `focus` / `source_requirements` |
| CRWD | 最近及上一收盘；盘前盘后 >1%；财报/下次财报、评级、产品、大单、管理层、法律 | `cybersecurity.toml` 的 `price_checks` / `news_categories` / `triggered_checks` |
| CRWD 行业 | Falcon、Charlotte AI、重大安全事件、PANW/S/ZS、并购 | CRWD `focus` + `industry_topics` |
| 宏观动态 | 美联储讲话、FedWatch、重要数据 actual/consensus/prior 与市场反应、AI/数据中心、关税/出口管制、NASDAQ/SOX | `macro_market.toml` 的 `news_categories` / `industry_topics` / `triggered_checks` / `price_checks` |
| 市场估值 | S&P 500 NTM PE；SOX PB/NTM PE；日期和来源；缺失处理 | `macro_market.toml` 的 `source_requirements` / `triggered_checks` |
| 市场情绪 | SOX/NDX 当前、1 月、3 月、年初比率；CNN Fear & Greed、VIX、AAII | `macro_market.toml` 的 `price_checks` / `triggered_checks` / `source_requirements` |

## 自动化防回归

`tests/test_modules.py` 直接断言公共六类股票新闻、各标的专属主题、盘前盘后阈值、GOOG 下跌原因、宏观缺失策略、中英文检索与来源策略。配置删漏上述项目会令测试失败。
