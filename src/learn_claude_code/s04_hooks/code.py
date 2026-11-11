"""
s04_hooks.py - Hooks

Hooks run callbacks at fixed points in the agent loop:

    User prompt
         |
         v
    UserPromptSubmit
         |
         v
    +----------+      +-------+      +------------+      +-------+
    | messages | ---> |  LLM  | ---> | PreToolUse | ---> | Tool  |
    +----------+      +---+---+      | permission |      +---+---+
         ^                | stop     | log        |          |
         |                v          +------------+          v
         |            Stop hook                         PostToolUse
         |                                               |
         +---------------- tool_result ------------------+
"""

import json
import os
import re
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# 命令行输入体验的兼容处理
try:
    import readline

    # #143 UTF-8 backspace fix for macOS libedit
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass


load_dotenv(override=True)


WORKDIR = Path.cwd()
cilent = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), max_retries=3)
MODEL = os.environ["MODEL_ID"]

ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax"
    if os.name == "nt"
    else "Unix-like: the shell tool runs the system shell"
)

INSTRUCTIONS = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use tools to solve tasks. Do not ask for approval yourself. "
    "For destructive operations, still request the tool call; "
    "the harness will approve or deny before execution."
)


# -- From s02-03: tool implementations --

def run_shell(command: str) -> str:
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,  # collect stdout & stderr
            text=True,  # handle the output as string in Python rather than bytes
            errors="replace",
            check=False,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = (WORKDIR / path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = (WORKDIR / path).resolve()
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    import glob as g

    try:
        matchs = sorted(
            {
                match
                for match in g.glob(pattern, root_dir=WORKDIR, recursive=True)
                if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
            }
        )
        shown = matchs[:200]
        if len(matchs) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as e:
        return f"Error: {e}"

TOOLS = [
    {
        "type": "function",
        "name": "shell",
        "description": "Run a shell command.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read file contents.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Write content to a file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "edit_file",
        "description": "Replace exact text in a file once",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "glob",
        "description": "Find files matching a glob pattern; ** matches recursively",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
]

TOOL_HANDLES = {
    "shell": run_shell,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# -- New in s04: hook system (s03 permission logic now uses hooks) --

# -- New in s03: three-gate permission pipeline --

# Gate 1: Hard deny list - always forbidden
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]


def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None


# Gate 2: Rule matching - context-dependent checks
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)


def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


PERMISSION_RULES = [
    {
        "tools": ["read_file", "write_file", "edit_file"],
        "check": lambda args: (
            not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR)
        ),
        "message": "Access outside workspace",
    },
    {
        "tools": ["shell"],
        "check": lambda args: (
            contains_destructive_command(args.get("command", ""))
            or any(
                kw in args.get("command", "") for kw in ["rm", "> /etc/", "chmod 777"]
            )
        ),
        "message": "Potentially destructive command",
    },
]


def check_rules(tool_name: str, args: dict) -> str | None:
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


# Gate 3: User approval - wait for comfirmation after rule match
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    print(f"\n⚠️   {reason}")
    print(f"    Tool: {tool_name} - {args}")
    choice = input("   Allow? [y/N]").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


# Pipeline: all three gate chained
def check_permission(block) -> bool:
    tool_name = block.name
    arguments = json.loads(block.arguments)
    if tool_name == "shell":
        reason = check_deny_list(arguments.get("command", ""))
        if reason:
            print(f"\n⛔️ {reason}")
            return False
    reason = check_rules(tool_name, arguments)
    if reason:
        decision = ask_user(tool_name, arguments, reason)
        if decision == "deny":
            return False

    return True


# -- Agent Loop: same as s02, with check_permission() inserted --


def agent_loop(inputs: list):
    for _ in range(5):
        response = cilent.responses.create(
            model=MODEL,
            instructions=INSTRUCTIONS,
            input=inputs,
            tools=TOOLS,
            max_output_tokens=8000,
        )

        # Expend assistant turn
        inputs.extend(response.output)

        # If the model didn't call a tool, we're done
        tool_calls = [
            block for block in response.output if block.type == "function_call"
        ]
        if not tool_calls:
            return response.output_text

        # Excute tools and return results
        results = []
        for block in tool_calls:
            # New in s03: run through permission pipeline before excuting
            if not check_permission(block):
                results.append(
                    {
                        "type": "function_call_output",
                        "call_id": block.call_id,
                        "output": "Permission denied.",
                    }
                )
                continue

            # 真正开始工具调用执行
            handler = TOOL_HANDLES.get(block.name)
            arguments: dict = json.loads(block.arguments)  # like {"command": "ls -la"}

            output = (
                handler(**arguments) if handler else f"Unknown handler: {block.name}"
            )

            print(f"Tool {block.name}'s output: {str(output)[:200]}")

            results.append(
                {
                    "type": "function_call_output",
                    "call_id": block.call_id,
                    "output": output,
                }
            )

        inputs.extend(results)

    raise RuntimeError("Exceed max iteration times: 3")


if __name__ == "__main__":
    print("s02: Tool Use - four tools added to s01")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input()
        except (EOFError, KeyboardInterrupt):
            break
        if not query.strip():
            continue
        if query.strip() in ("q", "exit"):
            break
        history.append(
            {
                "role": "user",
                "content": query,
            }
        )
        answer = agent_loop(history)
        # Print the model's final text response
        print(answer)
        print()
