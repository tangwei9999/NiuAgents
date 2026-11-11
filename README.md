# NiuAgents · 牛策

> A 股专用的多智能体选股与分析框架，Forked from [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)。

NiuAgents 在上游 TradingAgents 的多智能体分析引擎之上，做了一整套**面向 A 股的境内化改造**：接入 BaoStock / AKShare 四大数据源、把选股与深度分析全流程搬进国内网络能跑通的环境、加固海外端点的超时与回落，并新增一个从全市场扫描到 Top N 推荐的 **A 股选股器**。

<div align="center">

📊 [A 股选股器](#a-股选股器) | ⚡ [安装](#安装) | 🔑 [配置](#配置-api-key) | 🖥️ [使用](#使用) | 📚 [文档](#文档)

</div>

---

## 与上游的差异

| 维度 | 上游 TradingAgents | NiuAgents |
|------|--------------------|-----------|
| 数据源 | Yahoo / Alpha Vantage / Polymarket 等海外源为主 | **BaoStock + AKShare（东财 / 腾讯 / 新浪）四大境内源**，全链路不依赖 Yahoo 兜底 |
| 标的 | 美股 / 加密货币为主 | **A 股（沪深主板 / 中小板 / 创业板）** 一等公民 |
| 事件概率 | Polymarket（美国事件预测市场） | **`sse_option`**：由上交所 ETF 期权链反解隐含概率与隐含前向价 |
| 宏观数据 | FRED（美国宏观） | A 股链路默认关闭（`macro_data=none`），需要时一键改回 |
| 超时治理 | 自有 HTTP 分段超时 | 额外收紧 AKShare 库内默认超时、BaoStock 裸 socket 限时，并跳过连接失败的主机 |
| 选股 | 无 | **五步筛选漏斗 + 多智能体深度分析 + 综合评分 + 交易点位** |

---

## A 股选股器

`tradingagents screener` 从全 A 股扫描出发，经五步漏斗收敛到几十只潜力股，再逐一做多智能体深度分析，最终输出 Top N 推荐、综合评级与交易点位。

```
第 1 步  股票池（AKShare 全 A 股 ~5200 只）
第 2 步  基础过滤（剔除 ST / 科创板 688 / 北交所 8·4）
第 3 步  资金面预筛选（主力净流入 > 0，取前 100；盘前自动改用最近交易日）
第 4 步  消息面筛选（资金流入前 20 板块评估近 7 天利好）
第 5 步  技术面过滤（RSI 30-65、股价 > MA20、MACD 金叉、量比 ≥ 0.8）
   ↓  🎯 打印潜力股列表，交互确认（--yes 跳过）
第 6 步  批量深度分析（TradingAgents 多智能体，每只约 3-5 分钟）
第 7 步  综合评分排序（LLM 评级 40% + 技术面 30% + 资金面 20% + 动量 10%）
   ↓  🏆 最终推荐表 + 📌 逐只交易点位（阻力/支撑/建议买卖价）
第 8 步  保存 Top N 报告（评分摘要 + 每股完整报告树）
```

完整说明见 [tradingagents/screener/README.md](tradingagents/screener/README.md)。

---

## 安装（Windows）

> 需 Python 3.11 或更高版本（装好后用 `python --version` 确认）。以下命令在 **PowerShell** 中执行。

**1. 安装好 Python**（安装时勾选 “Add Python to PATH”）。

**2. cd 到项目根目录，创建虚拟环境**

```powershell
cd D:\java_projects\NiuAgents
python -m venv .venv
```

**3. 激活虚拟环境**

```powershell
.\.venv\Scripts\activate.bat
```

**4. 安装项目及依赖**

```powershell
pip install .
# 如果 pip install 太慢，可以用国内镜像：
pip install . -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
```

选股器另需两个境内数据源库（美股链路与单元测试不需要，未装时相关用例自动跳过）：

```powershell
pip install akshare baostock
```

**5. 验证安装成功**

```powershell
tradingagents --help
```

看到帮助信息即说明安装成功。

**6. 配置 API Key**

复制示例文件，编辑生成的 `.env`，填入你要用的**一家** LLM 提供商的 Key（A 股场景推荐 DeepSeek）：

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` 中设置（DeepSeek 示例）：

```dotenv
DEEPSEEK_API_KEY=sk-你的DeepSeek密钥
TRADINGAGENTS_LLM_PROVIDER=deepseek
TRADINGAGENTS_DEEP_THINK_LLM=deepseek-v4-pro
TRADINGAGENTS_QUICK_THINK_LLM=deepseek-flash
```

也支持 OpenAI / Google / Anthropic / Qwen / GLM / MiniMax / OpenRouter / Kimi / Groq / NVIDIA 等众多提供商，以及本地 Ollama 与远程 OpenAI 兼容端点，详见 [.env.example](.env.example)。

> **数据源无需 API Key**：BaoStock、AKShare（东财 / 腾讯 / 新浪）、上交所期权链全部免注册，DeepSeek 只需一个 Key 即可跑通完整选股与分析流程。

**7. 运行内置选股器（一键扫描全市场）**

```powershell
tradingagents screener --top 3 --workers 1
```

参数与报告结构详见下方 [使用](#使用)。

### （可选）Docker

```bash
cp .env.example .env   # 填入 API keys
docker compose run --rm tradingagents
```

更新仓库后用 `docker compose build` 重建镜像。结果、报告与缓存默认存在 `tradingagents_data` volume。

---

## 使用

### A 股选股器

```powershell
# 推荐 3 只（默认），建议 --workers 1 避免数据源限流
tradingagents screener --top 3 --workers 1

# 推荐 5 只
tradingagents screener --top 5 --workers 1

# 指定分析日期
tradingagents screener --top 3 --date 2026-11-09

# 无人值守：跳过深度分析前的确认
tradingagents screener --top 3 --workers 1 --yes
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--top N` | 推荐股票数量 | 3 |
| `--workers N` | 最大并发分析数（建议 1） | 3 |
| `--date YYYY-MM-DD` | 分析日期 | 今天 |
| `--save/--no-save` | 是否保存报告（摘要 + 每股完整报告树） | 保存 |
| `--html/--no-html` | 完整报告是否再渲一页 HTML | 生成 |
| `--yes` | 跳过深度分析前的确认 | 询问 |
| `--max-candidates N` | 深度分析的潜力股上限，按潜力评分降序取前 N | 50 |

报告保存在 `~/.tradingagents/logs/screener/<日期>/`：每股一份 `topN_代码_名称.md` 评分摘要，外加一个以代码命名的子目录，内含分析师到风控的完整报告树（`complete_report.md`，`--html` 时再配一页 `complete_report.html`）。

### 单只深度分析（CLI）

```powershell
# 交互式，按提示选择标的与日期
tradingagents

# 无交互，直接从参数运行
tradingagents --ticker 600519.SS --date 2026-11-10 --save --html
```

A 股代码后缀：沪市 `.SS`（如 `600519.SS`），深市 `.SZ`（如 `002371.SZ`）。

### Python 包

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

graph = TradingAgentsGraph(config=DEFAULT_CONFIG)
final_state, decision = graph.propagate("600519.SS", "2026-11-10")
print(decision)
```

---

## 文档

`docs/` 下配有可直接用浏览器打开的图文文档：

| 文档 | 内容 |
|------|------|
| [A 股改造指南](docs/a_stock_guide.html) | 境内数据源、超时治理、A 股标的与期权概率总体说明 |
| [选股器使用指南](docs/screener_guide.html) | 选股器五步漏斗、参数、报告结构、注意事项 |
| [架构图](docs/architecture_diagram.html) | 多智能体与数据源链路的分层架构 |
| [完整架构](docs/architecture_full.html) | 端到端数据流与模块交互全景 |
| [详细学习指南](docs/learning_guide_detailed.html) | 逐模块讲解，适合上手源码 |
| [学习计划](docs/learning_plan.html) | 分阶段阅读与练习路线 |

---

## 免责声明

本项目输出的评级、点位与推荐均为模型推理与机械计算结果，仅供研究与学习参考，**不构成任何投资建议**。市场有风险，决策需谨慎。

## 许可

本项目沿用上游 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 的许可条款，详见 [LICENSE](LICENSE)。
