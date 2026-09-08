"""AI Agent — Claude + 本地大模型 智能路由终端

双模型协作Agent：
  • Claude (Anthropic API) — 云端推理，复杂分析
  • Ollama本地模型 — 离线运算，隐私安全，快速响应

Agent模式：
  /auto   — 自动路由：简单问题用本地模型，复杂问题用Claude
  /think  — 深度思考：先用本地模型生成思路，再用Claude精炼答案
  /local  — 仅本地模型
  /claude — 仅Claude
  /compare — 双模型对比：同一问题两个模型都回答

Usage:
  python3 ai_agent.py              # 默认auto模式
  python3 ai_agent.py --local      # 启动时用本地模型
  python3 ai_agent.py --claude     # 启动时用Claude
  python3 ai_agent.py --model qwen2.5:72b  # 指定本地模型
"""

import os
import sys
import json
import time
import threading
from pathlib import Path
from datetime import datetime

# ── Env ──────────────────────────────────────────────────────────
_dotenv = Path(__file__).parent.parent / ".env"
if _dotenv.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_dotenv)
    except ImportError:
        pass

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
KIMI_API_BASE = os.getenv("KIMI_API_BASE", "https://api.moonshot.cn/v1")

# ── Colors ───────────────────────────────────────────────────────
GREEN  = "\033[92m"
BLUE   = "\033[94m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
MAGENTA = "\033[95m"
DIM    = "\033[2m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

# ── Ollama ───────────────────────────────────────────────────────
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# ── Config ───────────────────────────────────────────────────────
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
DEFAULT_LOCAL_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:72b")
DEFAULT_MODE = "auto"

# ── System prompts ──────────────────────────────────────────────
SYSTEM_PROMPT_STONE = """你是Stone 1.1量化交易系统的AI助手。你具备以下专长：

1. 策略分析：解读6档阶梯卖出、1分钟K线入场确认、二次入场等核心策略
2. 代码审查：检查Python交易代码的bug、竞态条件、状态一致性
3. 回测解读：分析回测结果，提出参数优化建议
4. 风控建议：评估止损、熔断器、仓位管理的有效性
5. 实盘支持：分析交易日志、订单状态、持仓数据

回复使用中文。涉及代码时给出具体文件和行号。"""

SYSTEM_PROMPT_GENERAL = """You are a helpful AI assistant. Respond in the same language the user uses.
你是一个有帮助的AI助手。用用户使用的语言回复。"""


# ══════════════════════════════════════════════════════════════════
# Model backends
# ══════════════════════════════════════════════════════════════════

class LocalModel:
    """Ollama local model backend."""

    def __init__(self, model: str = DEFAULT_LOCAL_MODEL):
        self.model = model
        self.host = OLLAMA_HOST

    def chat(self, messages: list, stream: bool = False) -> str:
        import urllib.request
        url = f"{self.host}/api/chat"
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": False,
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode())
                return data.get("message", {}).get("content", "")
        except Exception as e:
            raise RuntimeError(f"Ollama error: {e}")

    def list_models(self) -> list[str]:
        import urllib.request
        url = f"{self.host}/api/tags"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def is_available(self) -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as resp:
                return True
        except Exception:
            return False


class ClaudeModel:
    """Anthropic Claude cloud model backend."""

    def __init__(self, model: str = CLAUDE_MODEL):
        self.model = model
        self.api_key = ANTHROPIC_API_KEY

    def chat(self, messages: list) -> str:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set. Get one from https://console.anthropic.com/")
        from anthropic import Anthropic
        client = Anthropic(api_key=self.api_key)
        system_msg = ""
        chat_msgs = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                chat_msgs.append(m)
        response = client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_msg if system_msg else "You are a helpful assistant.",
            messages=chat_msgs,
        )
        return response.content[0].text

    def is_available(self) -> bool:
        return bool(self.api_key)


class KimiModel:
    """Kimi (Moonshot) cloud model backend."""

    def __init__(self, model: str = "kimi-k2.6"):
        self.model = model
        self.api_key = KIMI_API_KEY
        self.base_url = KIMI_API_BASE

    def chat(self, messages: list) -> str:
        if not self.api_key:
            raise RuntimeError("KIMI_API_KEY not set")
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=4096,
        )
        return response.choices[0].message.content

    def is_available(self) -> bool:
        return bool(self.api_key)


# ══════════════════════════════════════════════════════════════════
# Agent router
# ══════════════════════════════════════════════════════════════════

ROUTING_RULES = {
    "complex_keywords": [
        "审计", "bug", "修复", "代码审查", "策略优化", "回测分析",
        "debug", "fix", "audit", "refactor", "optimize",
        "架构", "设计", "竞态", "线程安全", "race condition",
        "复杂", "深度", "详细分析", "全面", "深度思考",
        "P0", "P1", "严重", "critical",
    ],
    "simple_keywords": [
        "你好", "hi", "hello", "快速", "简单", "quick",
        "确认", "检查一下", "帮我看看", "是什么",
        "参数", "配置", "当前", "状态",
    ],
    "stone_keywords": [
        "stone", "交易", "止损", "入场", "阶梯", "挂单",
        "alpaca", "gap", "ladder", "trailing", "re-entry",
        "仓位", "回撤", "tier", "K线", "bar",
    ],
    "privacy_keywords": [
        "API key", "密码", "密钥", "secret", "token",
        "账号", "资金", "持仓", "私钥",
    ],
}


class AgentRouter:
    """Decides which model to use based on query complexity."""

    def route(self, user_input: str, mode: str = "auto") -> dict:
        if mode == "local":
            return {"backend": "local", "reason": "用户指定本地模型"}
        if mode == "claude":
            return {"backend": "claude", "reason": "用户指定Claude"}
        if mode == "kimi":
            return {"backend": "kimi", "reason": "用户指定Kimi"}
        if mode == "think":
            return {"backend": "think", "reason": "深度思考模式: 本地→Claude"}

        # Auto routing
        lower = user_input.lower()

        # Privacy → always local
        for kw in ROUTING_RULES["privacy_keywords"]:
            if kw.lower() in lower:
                return {"backend": "local", "reason": f"包含隐私关键词'{kw}'，路由到本地模型"}

        # Complex → Claude if available, else local
        complex_hit = any(kw.lower() in lower for kw in ROUTING_RULES["complex_keywords"])
        simple_hit = any(kw.lower() in lower for kw in ROUTING_RULES["simple_keywords"])

        if complex_hit and not simple_hit:
            return {"backend": "claude", "reason": "复杂问题，路由到Claude"}

        # Stone-related + long query → Claude
        stone_hit = any(kw.lower() in lower for kw in ROUTING_RULES["stone_keywords"])
        if stone_hit and len(user_input) > 100:
            return {"backend": "claude", "reason": "交易系统深度问题，路由到Claude"}

        # Default: local model (fast, offline)
        return {"backend": "local", "reason": "常规问题，路由到本地模型(快速+离线)"}


# ══════════════════════════════════════════════════════════════════
# Agent core
# ══════════════════════════════════════════════════════════════════

class AIAgent:
    """Dual-model agent with auto routing, think mode, and compare mode."""

    def __init__(self, mode: str = DEFAULT_MODE, local_model: str = DEFAULT_LOCAL_MODEL):
        self.mode = mode
        self.local = LocalModel(local_model)
        self.claude = ClaudeModel()
        self.kimi = KimiModel()
        self.router = AgentRouter()
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT_STONE}]
        self.log_file = Path(__file__).parent / "agent_log.jsonl"

    def _choose_system_prompt(self, user_input: str) -> str:
        lower = user_input.lower()
        stone_hit = any(kw.lower() in lower for kw in ROUTING_RULES["stone_keywords"])
        return SYSTEM_PROMPT_STONE if stone_hit else SYSTEM_PROMPT_GENERAL

    def _update_system(self, prompt: str):
        self.messages[0]["content"] = prompt

    def chat_local(self, messages: list) -> str:
        return self.local.chat(messages)

    def chat_claude(self, messages: list) -> str:
        return self.claude.chat(messages)

    def chat_kimi(self, messages: list) -> str:
        return self.kimi.chat(messages)

    def think_mode(self, user_input: str) -> str:
        """Deep think: local model drafts思路 → Claude精炼答案"""
        print(f"{DIM}[思考步骤1] 本地模型生成思路...{RESET}")
        think_messages = [
            {"role": "system", "content": "你是一个分析助手。先列出关键要点，再给出初步思路。简洁输出。"},
            {"role": "user", "content": user_input},
        ]
        try:
            draft = self.chat_local(think_messages)
            print(f"{DIM}[思考步骤2] Claude基于思路精炼答案...{RESET}")
            refine_messages = [
                {"role": "system", "content": SYSTEM_PROMPT_STONE},
                {"role": "user", "content": f"以下是一个分析思路，请基于它给出完整、精确的答案:\n\n思路:\n{draft}\n\n原问题:\n{user_input}"},
            ]
            final = self.chat_claude(refine_messages)
            return f"{CYAN}【思路】{RESET}\n{draft}\n\n{GREEN}【精炼答案】{RESET}\n{final}"
        except Exception as e:
            # Fallback: if Claude fails, use local model directly
            print(f"{RED}Claude失败({e})，回退到本地模型直接回答{RESET}")
            direct_messages = [
                {"role": "system", "content": SYSTEM_PROMPT_STONE},
                {"role": "user", "content": user_input},
            ]
            return self.chat_local(direct_messages)

    def compare_mode(self, user_input: str) -> str:
        """Both models answer, show side by side."""
        local_result = ""
        claude_result = ""

        def get_local():
            try:
                msgs = [{"role": "system", "content": SYSTEM_PROMPT_STONE}, {"role": "user", "content": user_input}]
                local_result = self.chat_local(msgs)
            except Exception as e:
                local_result = f"[错误] {e}"

        def get_claude():
            try:
                msgs = [{"role": "system", "content": SYSTEM_PROMPT_STONE}, {"role": "user", "content": user_input}]
                claude_result = self.chat_claude(msgs)
            except Exception as e:
                claude_result = f"[错误] {e}"

        t1 = threading.Thread(target=get_local)
        t2 = threading.Thread(target=get_claude)
        print(f"{DIM}[对比模式] 双模型并行回答...{RESET}")
        t1.start()
        t2.start()
        t1.join(timeout=300)
        t2.join(timeout=60)

        # If threads timed out, show what we have
        if not local_result:
            local_result = "[超时] 本地模型响应超过300秒"
        if not claude_result:
            claude_result = "[超时或不可用] Claude响应超过60秒"

        sep = f"\n{BOLD}{'─'*60}{RESET}\n"
        return (
            f"{MAGENTA}【本地模型 ({self.local.model})】{RESET}\n{local_result}"
            + sep +
            f"{BLUE}【Claude ({self.claude.model})】{RESET}\n{claude_result}"
        )

    def process(self, user_input: str) -> str:
        """Route and process user input based on current mode."""
        route = self.router.route(user_input, self.mode)
        backend = route["backend"]
        reason = route["reason"]

        self._update_system(self._choose_system_prompt(user_input))

        if backend == "think":
            return self.think_mode(user_input)
        if backend == "compare":
            return self.compare_mode(user_input)

        print(f"{DIM}→ 路由: {reason}{RESET}")

        self.messages.append({"role": "user", "content": user_input})

        try:
            if backend == "local":
                reply = self.chat_local(self.messages)
            elif backend == "claude":
                reply = self.chat_claude(self.messages)
            elif backend == "kimi":
                reply = self.chat_kimi(self.messages)
            else:
                reply = self.chat_local(self.messages)
            self.messages.append({"role": "assistant", "content": reply})
            self._log(user_input, reply, backend)
            return reply
        except Exception as e:
            self.messages.pop()  # remove failed user message
            # Try fallback
            if backend != "local" and self.local.is_available():
                print(f"{YELLOW}主模型失败，回退到本地模型...{RESET}")
                self.messages.append({"role": "user", "content": user_input})
                try:
                    reply = self.chat_local(self.messages)
                    self.messages.append({"role": "assistant", "content": reply})
                    self._log(user_input, reply, "local_fallback")
                    return reply
                except Exception as e2:
                    self.messages.pop()
                    return f"{RED}所有模型均失败: 主={e}, 本地={e2}{RESET}"
            return f"{RED}模型失败: {e}{RESET}"

    def _log(self, user_input: str, reply: str, backend: str):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "backend": backend,
            "mode": self.mode,
            "user": user_input[:200],
            "reply": reply[:500],
        }
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# Terminal UI
# ══════════════════════════════════════════════════════════════════

def print_banner(agent: AIAgent):
    local_ok = agent.local.is_available()
    claude_ok = agent.claude.is_available()
    kimi_ok = agent.kimi.is_available()

    local_tag = f"{GREEN}✓{RESET}" if local_ok else f"{RED}✗{RESET}"
    claude_tag = f"{GREEN}✓{RESET}" if claude_ok else f"{RED}✗{RESET}"
    kimi_tag = f"{GREEN}✓{RESET}" if kimi_ok else f"{RED}✗{RESET}"

    models = agent.local.list_models()
    local_models_str = ", ".join(models[:5]) + (f"... ({len(models)} total)" if len(models) > 5 else "")

    print()
    print(f"{BOLD}{CYAN}╔══════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║   AI Agent — Claude + 本地大模型           ║{RESET}")
    print(f"{BOLD}{CYAN}║   智能路由 · 深度思考 · 双模型对比         ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════╝{RESET}")
    print()
    print(f"  模式: {BOLD}{YELLOW}{agent.mode}{RESET}")
    print(f"  本地模型: {local_tag} {agent.local.model} ({local_models_str})")
    print(f"  Claude:   {claude_tag} {agent.claude.model}")
    print(f"  Kimi:     {kimi_tag} {agent.kimi.model}")
    print()
    print(f"  {BOLD}命令:{RESET}")
    print(f"    /auto     — 自动路由（简单→本地，复杂→Claude）")
    print(f"    /think    — 深度思考（本地思路 → Claude精炼）")
    print(f"    /compare  — 双模型对比回答")
    print(f"    /local    — 仅本地模型")
    print(f"    /claude   — 仅Claude")
    print(f"    /kimi     — 仅Kimi")
    print(f"    /models   — 列出可用本地模型")
    print(f"    /switch <model> — 切换本地模型名")
    print(f"    /clear    — 清空对话")
    print(f"    /mode     — 显示当前模式")
    print(f"    /help     — 显示帮助")
    print(f"    /quit     — 退出")
    print()


def run_agent(mode: str = DEFAULT_MODE, local_model: str = DEFAULT_LOCAL_MODEL):
    agent = AIAgent(mode=mode, local_model=local_model)
    print_banner(agent)

    while True:
        mode_icon = {
            "auto": "⚡", "local": "🏠", "claude": "☁️", "kimi": "🌙",
            "think": "🧠", "compare": "⚖️",
        }
        icon = mode_icon.get(agent.mode, "?")

        try:
            user_input = input(f"{YELLOW}{icon} [{agent.mode}]{RESET} > ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{GREEN}再见！{RESET}")
            break

        if not user_input:
            continue

        cmd = user_input.lower().strip()

        # ── Commands ───────────────────────────────────────────
        if cmd in ("/quit", "/exit"):
            print(f"{GREEN}再见！{RESET}")
            break
        elif cmd == "/auto":
            agent.mode = "auto"
            print(f"{GREEN}→ 自动路由模式{RESET}")
        elif cmd == "/think":
            agent.mode = "think"
            print(f"{GREEN}→ 深度思考模式{RESET}")
        elif cmd == "/compare":
            agent.mode = "compare"
            print(f"{GREEN}→ 对比模式{RESET}")
        elif cmd == "/local":
            agent.mode = "local"
            print(f"{GREEN}→ 仅本地模型模式{RESET}")
        elif cmd == "/claude":
            agent.mode = "claude"
            print(f"{GREEN}→ 仅Claude模式{RESET}")
        elif cmd == "/kimi":
            agent.mode = "kimi"
            print(f"{GREEN}→ 仅Kimi模式{RESET}")
        elif cmd == "/mode":
            print(f"  当前模式: {YELLOW}{agent.mode}{RESET}")
            print(f"  本地模型: {agent.local.model}")
            print(f"  Claude:   {agent.claude.model}")
            print(f"  Kimi:     {agent.kimi.model}")
        elif cmd == "/models":
            models = agent.local.list_models()
            if models:
                print(f"  {GREEN}可用本地模型:{RESET}")
                for m in models:
                    marker = f"{CYAN}●{RESET}" if m == agent.local.model else f"{DIM}○{RESET}"
                    print(f"    {marker} {m}")
            else:
                print(f"  {RED}无法获取本地模型列表{RESET}")
        elif cmd.startswith("/switch"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2:
                new_model = parts[1].strip()
                agent.local.model = new_model
                print(f"{GREEN}→ 本地模型切换为 {new_model}{RESET}")
            else:
                print(f"{RED}用法: /switch <model_name>{RESET}")
        elif cmd == "/clear":
            agent.messages = [{"role": "system", "content": SYSTEM_PROMPT_STONE}]
            print(f"{GREEN}对话已清空{RESET}")
        elif cmd == "/help":
            print_banner(agent)
        else:
            # ── Chat ──────────────────────────────────────────
            start_time = time.time()
            try:
                if agent.mode == "compare":
                    reply = agent.compare_mode(user_input)
                elif agent.mode == "think":
                    reply = agent.think_mode(user_input)
                else:
                    reply = agent.process(user_input)
                elapsed = time.time() - start_time
                print(f"\n{GREEN}{reply}{RESET}")
                print(f"{DIM}[{elapsed:.1f}s]{RESET}\n")
            except Exception as e:
                elapsed = time.time() - start_time
                print(f"{RED}错误: {e}{RESET} {DIM}[{elapsed:.1f}s]{RESET}\n")


# ══════════════════════════════════════════════════════════════════
# CLI entry
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mode = DEFAULT_MODE
    local_model_name = DEFAULT_LOCAL_MODEL

    for arg in sys.argv[1:]:
        a = arg.lower()
        if a in ("--local", "-l"):
            mode = "local"
        elif a in ("--claude", "-c"):
            mode = "claude"
        elif a in ("--kimi", "-k"):
            mode = "kimi"
        elif a in ("--think", "-t"):
            mode = "think"
        elif a in ("--compare", "--cmp"):
            mode = "compare"
        elif a in ("--auto", "-a"):
            mode = "auto"
        elif a.startswith("--model="):
            local_model_name = a.split("=", 1)[1]
        elif a == "--model" and len(sys.argv) > sys.argv.index(arg) + 1:
            local_model_name = sys.argv[sys.argv.index(arg) + 1]

    run_agent(mode=mode, local_model=local_model_name)
