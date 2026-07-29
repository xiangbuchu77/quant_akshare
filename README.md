# Quant AKShare Dashboard

一个本地运行的 A 股持仓与自选股决策看板。项目使用 AKShare 等公开行情源，
提供实时行情、持仓账本、买卖记录、资金与量能观察、K 线/分时图、今日盈亏、
做 T 辅助建议，以及按需触发的 DeepSeek 分析。

本项目只用于行情研究和交易辅助，不连接券商、不自动下单，也不承诺收益。

## 主要功能

- 本地持仓、自选股和成交日志原子化保存，刷新页面不会丢失。
- 网页新增/删除持仓和自选股；新增持仓默认记作当日买入，并支持买卖与撤销记录。
- 腾讯行情实时刷新，结合 AKShare/东方财富数据展示资金、板块、量能和支撑位。
- 宝妈指数综合股吧语义、东财热度、雪球讨论和微博舆情，默认权重为
  60% / 15% / 20% / 5%；缺失来源会在可用来源间自动重分配权重。
- 交互式近 30 日 K 线与当日分时线。
- 账户总资产口径与持仓估算口径分开展示，并拆分已实现、浮动和做 T 盈亏。
- 根据成本、波动、分时和市场状态动态生成参考止损与目标。
- DeepSeek 分析和简短对话只在点击按钮时调用。
- QClaw/OpenClaw API-only skill，可用自然语言管理组合并打开看板。
- 日线缓存、均线回测、信号和命令行实时监控。

## 环境要求

- Python 3.10 或更高版本
- macOS 或 Linux；核心 Python 服务也可在 Windows 上运行
- 可访问所使用的公开行情接口

## 快速开始

```bash
git clone https://github.com/xiangbuchu77/quant_akshare.git
cd quant_akshare

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

启动看板服务：

```bash
quant-akshare qclaw-service
```

也可以不使用命令行入口：

```bash
python -m quant_akshare.cli qclaw-service
```

浏览器打开：

```text
http://127.0.0.1:18766/dashboard
```

首次启动时组合为空，可直接在网页的“股票管理”中新增持仓或自选股。

## DeepSeek 配置

AI 功能是可选的。不配置密钥时，行情、账本和看板功能仍可正常使用。

方式一：使用本地环境文件：

```bash
cp .env.example .env.local
```

编辑 `.env.local`，填入自己的 `DEEPSEEK_API_KEY`。该文件已被 Git 忽略。

方式二：使用用户私有配置：

```bash
mkdir -p ~/.config/quant_akshare
printf '%s\n' '你的 DeepSeek API Key' \
  > ~/.config/quant_akshare/deepseek_api_key.txt
chmod 600 ~/.config/quant_akshare/deepseek_api_key.txt
```

方式三：启动前设置环境变量：

```bash
export DEEPSEEK_API_KEY='你的 DeepSeek API Key'
export DEEPSEEK_MODEL='deepseek-chat'
```

看板服务会直接提供 AI 分析接口；也可以单独运行：

```bash
quant-akshare ai-server
```

## QClaw / OpenClaw

技能源码位于：

```text
qclaw_skill/a-stock-quant-dashboard
```

macOS 上可将技能链接到 QClaw 的默认技能目录：

```bash
QCLAW_SKILLS_DIR="$HOME/Library/Application Support/QClaw/openclaw/config/skills"
mkdir -p "$QCLAW_SKILLS_DIR"
ln -sfn "$PWD/qclaw_skill/a-stock-quant-dashboard" \
  "$QCLAW_SKILLS_DIR/a-stock-quant-dashboard"
```

然后运行：

```bash
bash qclaw_skill/a-stock-quant-dashboard/scripts/start_service.sh
```

统一动作接口：

```text
POST http://127.0.0.1:18766/qclaw/message
```

常用动作包括：

- `import_holdings`：按券商快照导入持仓。
- `import_watchlist` / `remove_watchlist`：新增或删除自选股。
- `apply_trade` / `undo_trade`：记录或撤销买卖。
- `update_risk_lines` / `clear_risk_lines`：覆盖或恢复自动风控线。
- `update_account_snapshot`：保存券商总资产、总市值和当日盈亏基准。
- `reorder_symbols`：调整看板排列顺序。
- `generate_dashboard` / `open_dashboard`：生成或打开看板。
- `dashboard_snapshot`：读取组合、行情和盈亏快照。
- `analyze` / `chat`：按需调用 DeepSeek。

完整参数和对话规则见
[`qclaw_skill/a-stock-quant-dashboard/SKILL.md`](qclaw_skill/a-stock-quant-dashboard/SKILL.md)。

## 研究命令

拉取并缓存前复权日线：

```bash
quant-akshare fetch --symbol 000001 --start 20240101 --end 20261231
```

回测 20/60 日均线策略：

```bash
quant-akshare backtest \
  --symbol 000001 \
  --start 20240101 \
  --end 20261231 \
  --short-window 20 \
  --long-window 60
```

生成最新信号：

```bash
quant-akshare signal --symbol 000001 --start 20240101 --end 20261231
```

盘中监控示例：

```bash
quant-akshare watch \
  --symbols 000001,600000 \
  --costs 000001=10.50 \
  --shares 000001=100 \
  --interval 5
```

这些命令只生成数据、信号或提醒，不会向券商下单。

## 数据与隐私

下列内容只保存在本机，并由 `.gitignore` 排除：

- `data/`：持仓主账本、成交日志、行情缓存和每日快照。
- `reports/`：生成的 HTML、回测 CSV 和 AI 分析。
- `.env.local`：本机 API 配置。
- `qclaw_agent_workspace/`：QClaw 的本机工作区与记忆。

公开仓库中只保留空目录占位文件。提交前仍建议运行：

```bash
git status --short
```

不要上传券商截图、账户信息、真实持仓导出或任何 API Key。更多说明见
[`SECURITY.md`](SECURITY.md)。

## 项目结构

```text
quant_akshare/       Python 包与本地 HTTP 服务
qclaw_skill/         QClaw/OpenClaw 技能
tests/               单元测试
data/                本机运行数据，Git 默认忽略
reports/             生成结果，Git 默认忽略
pyproject.toml       包元数据与依赖
```

## 测试

```bash
python -m unittest discover -s tests
```

## 免责声明

公开行情接口可能延迟、限流、变更或返回不完整数据。看板中的资金流、支撑位、
止损、目标、情绪和 AI 文本均为辅助信息，不构成投资建议。请以券商成交记录和
交易所数据为准，并自行承担交易风险。

## License

[MIT](LICENSE)
