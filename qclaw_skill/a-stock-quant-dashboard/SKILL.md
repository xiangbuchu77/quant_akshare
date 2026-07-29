---
name: a-stock-quant-dashboard
description: A股持仓与自选股看板助手。用于导入持仓、导入自选股、生成实时 HTML 看板，并按需触发 DeepSeek 分析。
---

# A股持仓与自选股看板

这是一个 QClaw/OpenClaw API-only skill。QClaw 负责自然语言对话和触发动作；本地服务负责维护持仓/自选股状态、生成 HTML 看板、按需调用 DeepSeek。

## 服务

启动服务：

```bash
scripts/start_service.sh
```

健康检查：

```bash
curl http://127.0.0.1:18766/healthz
```

统一入口：

```text
POST http://127.0.0.1:18766/qclaw/message
```

## 动作

### 导入持仓

```json
{
  "action": "import_holdings",
  "payload": {
    "holdings": [
      {"symbol": "002463", "cost": 147.635, "shares": 200}
    ]
  }
}
```

`stop` 和 `target` 是可选覆盖项。默认不要要求用户手填；生成/打开看板时会根据实时行情、成本、日内波动和盈亏状态自动计算参考止损和目标。

### 导入自选

```json
{
  "action": "import_watchlist",
  "payload": {
    "symbols": ["300750", "000725"]
  }
}
```

### 生成看板

```json
{"action": "generate_dashboard"}
```

返回的 `dashboard` 是生成的 HTML 文件路径。浏览器访问地址：

```text
http://127.0.0.1:18766/dashboard
```

### 打开看板

```json
{"action": "open_dashboard"}
```

返回看板路径。若运行环境支持打开本地文件，可直接打开该 HTML；否则把路径回复给用户。

### 删除持仓

```json
{
  "action": "remove_holdings",
  "payload": {
    "symbols": ["002463"]
  }
}
```

### 删除自选

```json
{
  "action": "remove_watchlist",
  "payload": {
    "symbols": ["300750"]
  }
}
```

### 清空自选

```json
{"action": "clear_watchlist"}
```

### 记录买卖

```json
{
  "action": "apply_trade",
  "payload": {
    "symbol": "000725",
    "side": "buy",
    "price": 6.92,
    "shares": 900
  }
}
```

网页买入/卖出也会调用这个动作。服务启动时，交易会写入 `data/qclaw_portfolio.json`，流水写入 `data/qclaw_trades.jsonl`。

### 修改止损/目标

```json
{
  "action": "update_risk_lines",
  "payload": {
    "lines": [
      {"symbol": "002463", "stop": 135, "target": 148}
    ]
  }
}
```

这是显式覆盖线。用户没有明确指定时，仍然使用自动线。

### 恢复自动止损/目标

```json
{
  "action": "clear_risk_lines",
  "payload": {
    "symbols": ["002463"]
  }
}
```

不传 `symbols` 时清空全部覆盖线。

### 调整排序

```json
{
  "action": "reorder_symbols",
  "payload": {
    "symbols": ["000725", "002463", "600183", "605006", "300750"]
  }
}
```

未列出的股票会自动排在后面，当前顺序会写入状态文件。

### 更新账户盈亏基准

```json
{
  "action": "update_account_snapshot",
  "payload": {
    "totalAssets": 80531.62,
    "marketValue": 33892.00,
    "reportedDailyPnl": 326.00,
    "netTransfer": 0
  }
}
```

券商“当日参考盈亏”按账户总资产计算。`netTransfer` 表示当日净转入，转入为正、
转出为负。保存后看板会以账户口径为主，同时保留持仓与成交记录估算值供核对。

### 触发 DeepSeek 分析

```json
{"action": "analyze"}
```

启动 `scripts/start_service.sh` 时会自动确保本地 `ai-server` 在运行。DeepSeek API Key 读取顺序为环境变量 `DEEPSEEK_API_KEY`、项目 `.env.local` / `.env`、用户私有文件 `~/.config/quant_akshare/deepseek_api_key.txt`。

## 对话原则

- 用户说“导入我的持仓”时，向用户确认股票代码、成本、股数，然后调用 `import_holdings`。这表示按券商当前持仓结果覆盖本地状态。
- 用户说“又买了/买入/卖出/加仓/减仓”时，必须调用 `apply_trade`、`buy` 或 `sell`，不能调用 `import_holdings`。买入会在原股数上累加并重算加权成本，卖出会扣减股数。
- 默认不要让用户手动填写止损/目标；除非用户明确指定，否则让本地服务在 `generate_dashboard` / `open_dashboard` 时自动生成。
- 用户说“导入自选股”时，只需要股票代码列表，然后调用 `import_watchlist`。
- 用户说“删除持仓 / 移除持仓”时，确认代码后调用 `remove_holdings`。
- 用户说“删除自选 / 移除自选”时，确认代码后调用 `remove_watchlist`。
- 用户说“清空自选”时，调用 `clear_watchlist`。
- 用户说“买入/卖出”时，确认代码、方向、价格、股数后调用 `apply_trade`。例如用户“京东方 A 本来 900 股，又买 1200 股”代表最终 2100 股，不是覆盖成 1200 股。
- 用户说“修改止损/止盈/目标”时，调用 `update_risk_lines`，这是显式覆盖；用户没说就不要手动设置。
- 用户说“恢复自动止损/目标”时，调用 `clear_risk_lines`。
- 用户说“调整顺序/排序”时，调用 `reorder_symbols`。
- 用户提供券商总资产、总市值和当日盈亏时，调用 `update_account_snapshot`。
- 用户说“生成看板 / 更新看板”时，调用 `generate_dashboard`。
- 用户说“启动看板 / 打开看板”时，调用 `open_dashboard`，并把 HTML 路径返回给用户。
- 用户说“帮我分析 / DeepSeek 分析 / AI 分析”时，调用 `analyze`。
- 分析只做辅助，不声称保证盈利，不自动下单。
