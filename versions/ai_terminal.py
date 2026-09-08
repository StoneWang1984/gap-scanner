"""AI Terminal — Claude + Kimi dual-model chat interface.

Usage:
  python3 ai_terminal.py           # Interactive mode (default: Claude)
  python3 ai_terminal.py --kimi    # Start with Kimi
  python3 ai_terminal.py --claude  # Start with Claude

Commands inside chat:
  /claude    — switch to Claude
  /kimi      — switch to Kimi
  /model     — show current model
  /clear     — clear conversation history
  /help      — show commands
  /quit      — exit
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
KIMI_API_BASE = os.getenv("KIMI_API_BASE", "https://api.moonshot.cn/v1")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

MODELS = {
    "claude": {
        "name": "Claude (Anthropic)",
        "default_model": "claude-sonnet-4-20250514",
    },
    "kimi": {
        "name": "Kimi (Moonshot)",
        "default_model": "kimi-k2.6",
        "models": ["kimi-k2.6", "kimi-k2.7-code"],
    },
}

GREEN = "\033[92m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"


def chat_claude(messages: list, model: str) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    # Convert messages: Anthropic expects separate system + messages
    system_msg = ""
    chat_msgs = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            chat_msgs.append(m)

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_msg if system_msg else "You are a helpful assistant.",
        messages=chat_msgs,
    )
    return response.content[0].text


def chat_kimi(messages: list, model: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=KIMI_API_KEY, base_url=KIMI_API_BASE)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=4096,
    )
    return response.choices[0].message.content


def print_banner(current: str):
    m = MODELS[current]
    print()
    print(f"{BOLD}{BLUE}╔══════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{BLUE}║  AI Terminal — Claude + Kimi            ║{RESET}")
    print(f"{BOLD}{BLUE}╚══════════════════════════════════════════╝{RESET}")
    print(f"  Current: {GREEN}{m['name']}{RESET} ({m['default_model']})")
    print(f"  Commands: /claude  /kimi  /model  /clear  /help  /quit")
    print()


def run_terminal(start_model: str = "claude"):
    current = start_model
    messages = [
        {"role": "system", "content": "You are a helpful assistant. Respond in the same language the user uses."}
    ]

    print_banner(current)

    while True:
        try:
            user_input = input(f"{YELLOW}[{MODELS[current]['name']}]{RESET} > ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{GREEN}Goodbye!{RESET}")
            break

        if not user_input:
            continue

        cmd = user_input.lower()

        if cmd == "/quit" or cmd == "/exit":
            print(f"{GREEN}Goodbye!{RESET}")
            break
        elif cmd == "/claude":
            if ANTHROPIC_API_KEY:
                current = "claude"
                print(f"{GREEN}Switched to Claude{RESET}")
            else:
                print(f"{RED}ANTHROPIC_API_KEY not set in .env{RESET}")
        elif cmd == "/kimi":
            if KIMI_API_KEY:
                current = "kimi"
                print(f"{GREEN}Switched to Kimi{RESET}")
            else:
                print(f"{RED}KIMI_API_KEY not set in .env{RESET}")
        elif cmd == "/model":
            m = MODELS[current]
            print(f"  Current: {m['name']} ({m['default_model']})")
        elif cmd == "/clear":
            messages = [
                {"role": "system", "content": "You are a helpful assistant. Respond in the same language the user uses."}
            ]
            print(f"{GREEN}Conversation cleared.{RESET}")
        elif cmd == "/help":
            print("  /claude  — switch to Claude (Anthropic)")
            print("  /kimi    — switch to Kimi (Moonshot)")
            print("  /model   — show current model info")
            print("  /clear   — clear conversation history")
            print("  /help    — show this help")
            print("  /quit    — exit terminal")
        else:
            # Send to current model
            messages.append({"role": "user", "content": user_input})
            try:
                if current == "claude":
                    reply = chat_claude(messages, MODELS["claude"]["default_model"])
                else:
                    reply = chat_kimi(messages, MODELS["kimi"]["default_model"])
                messages.append({"role": "assistant", "content": reply})
                print(f"\n{GREEN}{reply}{RESET}\n")
            except Exception as e:
                print(f"{RED}Error: {e}{RESET}")
                # Remove failed exchange from history
                messages.pop()


if __name__ == "__main__":
    start = "claude"
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower().replace("--", "")
        if arg == "kimi":
            start = "kimi"
        elif arg == "claude":
            start = "claude"
    run_terminal(start)
