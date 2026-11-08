import json
import os
import subprocess

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


cilent = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), max_retries=3)
MODEL = os.environ["MODEL_ID"]

ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)

INSTRUCTIONS = (
    f"You are a coding agent at {os.getcwd()}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use shell to solve tasks. Act, don't explain."
)


# -- Tool definition --
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
    }
]


# -- Tool excution --
def run_shell(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,  # collect stdout & stderr
            text=True,  # handle the output as string in Python rather than bytes
            errors="replace",
            check=False,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "no output"
    except subprocess.TimeoutExpired:
        return "Error: Timeout(120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def agent_loop(inputs: list):
    for _ in range(3):
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
            # Parse JSON to Dict
            arguments: dict = json.loads(block.arguments)  # like {"command": "ls -la"}
            print(f"Want to use parameters: {arguments} to excute tool: {block.name}")

            output = run_shell(arguments["command"])
            print(output[:200])

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
    print("s01: Agent Loop")
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
