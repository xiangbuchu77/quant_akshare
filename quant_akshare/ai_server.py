from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
LOCAL_KEY_PATHS = (
    PROJECT_ROOT / ".env.local",
    PROJECT_ROOT / ".env",
    Path.home() / ".config" / "quant_akshare" / "deepseek_api_key.txt",
)


def build_prompt(snapshot: dict[str, Any]) -> str:
    return (
        "你是A股短线盯盘辅助分析员，服务对象是偏短线交易者。请基于用户当前持仓、"
        "行情、大盘状态、板块资金流、个股主力资金、量能、支撑位、动态线和分时强弱，"
        "给出简洁、可执行、风险优先的盘中分析。不要预测必涨必跌，不要承诺收益，"
        "不要建议满仓梭哈，不要说自己可以自动下单。"
        "\n\n分析原则："
        "\n- 先判断大盘和板块是否支持进攻；弱势时默认防守。"
        "\n- 短线优先看价格是否站上分时均价线、是否冲高回落、尾盘是否承接。"
        "\n- 放量上涨且资金流入才偏进攻；放量下跌或跌破均价线要收缩。"
        "\n- 盈利票优先保护利润，亏损票不轻易补仓，风险票给明确触发条件。"
        "\n- ETF和个股分开判断，ETF更重视指数/板块趋势，个股更重视资金与分时承接。"
        "\n- 输出股票时优先使用股票名称，不要使用股票代码；除非名称缺失或重名，才在名称后补代码。"
        "\n\n请按这个格式："
        "\n1. 盘面结论：一句话说明今天偏进攻、震荡还是防守。"
        "\n2. 持仓优先级：按风险从高到低列出最需要盯的1-3只。"
        "\n3. 分时信号：说明每只重点票是站上均价、跌破均价、冲高回落还是承接尚可。"
        "\n4. 资金与量能：说明主力流入/流出、放量/缩量是否支持继续持有。"
        "\n5. 操作计划：每只持仓给出持有、减仓观察、跌破卖出、分批止盈、等待，不超过一句。"
        "\n6. 风险提醒：列出最明确的失效条件。"
        "\n\n输出中文，尽量短，控制在8条以内；禁止使用Markdown粗体符号；不要用股票编号代替股票名称。"
        "\n\n当前快照JSON：\n"
        + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    )


def build_chat_prompt(snapshot: dict[str, Any], question: str, history: list[dict[str, str]] | None = None) -> str:
    trimmed_history = (history or [])[-6:]
    return (
        "你是A股短线盯盘助手，只回答用户围绕当前看板的简短问题。"
        "回答要短、直接、风险优先，不承诺收益，不说自动下单。"
        "优先使用股票名称，不要用股票代码代替股票名称。"
        "如果问题超出当前快照能判断的范围，要明确说需要用户自己确认盘口或成交。"
        "\n\n输出要求：中文，最多4句话；如涉及操作，只给观察条件和风控条件。"
        "\n\n最近对话：\n"
        + json.dumps(trimmed_history, ensure_ascii=False, separators=(",", ":"))
        + "\n\n当前快照JSON：\n"
        + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        + "\n\n用户问题："
        + question.strip()
    )


def analyze_with_deepseek(snapshot: dict[str, Any]) -> str:
    return _complete_with_deepseek(
        [
            {
                "role": "system",
                "content": "你只做证券盯盘辅助分析，必须强调风险控制，不能声称保证盈利。",
            },
            {"role": "user", "content": build_prompt(snapshot)},
        ],
        max_tokens=900,
    )


def chat_with_deepseek(snapshot: dict[str, Any], question: str, history: list[dict[str, str]] | None = None) -> str:
    question = question.strip()
    if not question:
        raise RuntimeError("question is empty")
    return _complete_with_deepseek(
        [
            {
                "role": "system",
                "content": "你只做证券盯盘短问答，回答必须简短、谨慎、以风险控制为先。",
            },
            {"role": "user", "content": build_chat_prompt(snapshot, question, history)},
        ],
        max_tokens=360,
    )


def _complete_with_deepseek(messages: list[dict[str, str]], max_tokens: int) -> str:
    api_key = load_deepseek_api_key()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set and no local key file was found")
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
        "messages": messages,
        "thinking": {"type": "disabled"},
        "max_tokens": max_tokens,
        "stream": False,
    }
    session = requests.Session()
    session.trust_env = False
    response = session.post(
        DEEPSEEK_CHAT_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"DeepSeek HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("DeepSeek returned no choices")
    return str(choices[0].get("message", {}).get("content") or "").strip()


def load_deepseek_api_key() -> str | None:
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        return env_key.strip()
    for path in LOCAL_KEY_PATHS:
        key = _read_key_file(path)
        if key:
            return key
    return None


def _read_key_file(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    if path.name.startswith(".env"):
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "DEEPSEEK_API_KEY":
                return value.strip().strip('"').strip("'") or None
        return None
    return text.splitlines()[0].strip() or None


class AiAnalysisHandler(BaseHTTPRequestHandler):
    server_version = "QuantAiServer/0.1"

    def do_OPTIONS(self) -> None:
        self._send_empty(204)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json({"ok": True})
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        if self.path not in {"/analyze", "/chat"}:
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 200_000:
                raise ValueError("invalid request body size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/chat":
                snapshot = payload.get("snapshot") if isinstance(payload, dict) else {}
                question = str(payload.get("question") or "") if isinstance(payload, dict) else ""
                history = payload.get("history") if isinstance(payload, dict) and isinstance(payload.get("history"), list) else []
                answer = chat_with_deepseek(snapshot if isinstance(snapshot, dict) else {}, question, history)
                self._send_json({"answer": answer, "provider": "deepseek"})
                return
            snapshot = payload if isinstance(payload, dict) else {}
            analysis = analyze_with_deepseek(snapshot)
            self._send_json({"analysis": analysis, "provider": "deepseek"})
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self._cors_headers()
        self.end_headers()

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


def run_ai_server(host: str = "127.0.0.1", port: int = 18765) -> None:
    server = ThreadingHTTPServer((host, port), AiAnalysisHandler)
    print(f"AI analysis server: http://{host}:{port}")
    print(f"DeepSeek model: {os.environ.get('DEEPSEEK_MODEL', DEFAULT_DEEPSEEK_MODEL)}")
    print("DeepSeek API key: detected" if load_deepseek_api_key() else "DeepSeek API key: missing")
    print("Press Ctrl+C to stop.")
    server.serve_forever()
