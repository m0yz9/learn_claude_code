# s01：用 Responses API 实现 Agent Loop

本章参考 [learn-claude-code 的 s01 中文说明](https://github.com/shareAI-lab/learn-claude-code/blob/main/s01_agent_loop/README.zh.md)，按本目录的 [code.py](./code.py) 重新整理。课程使用 Anthropic Messages API；这里使用 OpenAI Python 客户端与 Responses API，消息格式不能直接照搬。

## 要解决的问题

模型提出命令，不代表命令已经执行。本地程序需要接收工具调用、执行命令、把结果交回模型，再让模型决定下一步。这一层执行框架就是本章的 agent harness。

```text
用户输入 → 请求模型 → 保存模型输出
                         ↓
                   有没有工具调用？
                    /           \
                  没有           有
                   ↓             ↓
                返回正文      执行 Shell
                                 ↓
                            保存工具结果
                                 ↓
                            再次请求模型
```

当前实现每个用户问题最多请求模型三次。第三次回复如果仍要求执行工具，程序执行并保存结果后抛出 `RuntimeError`，不会再请求第四次。

## 先看对象和职责

| 名称 | 当前代码中的职责 |
|---|---|
| `cilent` | `OpenAI` 客户端实例；代码目前采用这个拼写 |
| `SYSTEM` | 通过 `instructions` 传入的行为说明 |
| `TOOLS` | 告诉模型工具的名称、用途与参数结构，不负责执行 |
| `history` / `inputs` | 同一个列表，保存用户消息、模型输出项和工具结果 |
| `response` | 一次 API 请求返回的响应对象 |
| `response.output` | 本轮输出项列表，可包含消息、函数调用及推理相关项 |
| `response.output_text` | SDK 提取出的文本输出，供终端打印 |
| `run_shell()` | 真正调用本机 Shell 执行命令 |
| `results` | 本轮所有工具调用的结果列表，每轮重新创建 |

`agent_loop(history)` 没有复制历史列表，因此函数内的 `inputs.extend(...)` 会直接更新外部的 `history`。`results` 只暂存当前轮结果，跨轮完整记录保存在 `history` 中。

## OpenAI 客户端和环境变量

当前代码：

```python
load_dotenv(override=True)
cilent = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), max_retries=3)
MODEL = os.environ["MODEL_ID"]
```

`load_dotenv()` 加载 `.env`；`override=True` 允许文件中的配置覆盖已有同名环境变量。`os.getenv()` 在缺失时返回 `None`，`os.environ["MODEL_ID"]` 缺失时会抛出 `KeyError`。

`max_retries=3` 是客户端对可重试请求失败的重试设置，与下面的三轮 Agent 循环是两回事。三轮表示三次逻辑模型调用，不一定只有三次网络尝试。

## 按实际代码走一轮

### 1. 保存用户消息

```python
history.append({"role": "user", "content": query})
answer = agent_loop(history)
print(answer)
```

外层 `while True` 负责多轮聊天：空输入跳过，`q`、`exit`、EOF 或键盘中断退出输入循环。内层 `agent_loop()` 负责完成当前问题的工具交互。

### 2. 请求模型

```python
response = cilent.responses.create(
    model=MODEL,
    instructions=SYSTEM,
    input=inputs,
    tools=TOOLS,
    max_output_tokens=8000,
)
```

`input` 传完整历史，`instructions` 传系统说明，`tools` 提供工具定义。当前代码没有使用 `previous_response_id`，而是每次主动传入历史。

工具定义中的 `name="shell"` 是模型看到的名称；本地函数叫 `run_shell`。当前执行器只有一个工具，所以直接调用该函数，没有按名称分发。

`parameters` 是 JSON Schema：要求参数为对象，必须有字符串字段 `command`，且不接受额外字段。模型生成参数，本地程序执行参数对应的操作。

### 3. 保存输出并判断是否继续

```python
inputs.extend(response.output)
tool_calls = [
    block for block in response.output
    if block.type == "function_call"
]
if not tool_calls:
    return response.output_text
```

必须保留模型输出的原有结构，不能把整个 `response.output` 塞进一条 assistant 消息的 `content`。工具调用本身也是历史的一部分。

`extend()` 将列表中的项目逐个加入；`append(response.output)` 会多套一层列表。先保存再判断，才能同时保存没有工具调用时的最终回答。

当前代码以“没有函数调用”为停止条件，没有额外检查响应是否因输出限制等原因未完整结束。

### 4. 解析参数并执行命令

```python
arguments = json.loads(block.arguments)
output = run_shell(arguments["command"])
```

参数的数据形状依次是：

```text
'{"command":"ls -la"}'   JSON 字符串
          ↓ json.loads
{"command": "ls -la"}    Python 字典
          ↓ ["command"]
"ls -la"                 交给 Shell 的命令
```

当前 `for block in tool_calls` 按顺序执行。一次模型回复可以包含多个调用，但本程序没有并发调度。模型也要等工具结果回传后才能看到执行结果。

### 5. 回传工具结果

```python
results.append({
    "type": "function_call_output",
    "call_id": block.call_id,
    "output": output,
})
inputs.extend(results)
```

`call_id` 对应前面的工具调用，`output` 是本地执行结果字符串。工具结果是独立输入项，不包装成 `role="user"` 消息。

下一轮模型请求看到的结构类似：

```text
history
 ├── 用户消息
 ├── 模型输出项：function_call（包含 call_id）
 ├── 工具结果项：function_call_output（对应同一 call_id）
 └── 后续模型输出项
```

接口字段与回传规则可对照 [OpenAI 官方 Function calling 文档](https://developers.openai.com/api/docs/guides/function-calling)。

## 与课程原版的字段对照

| 课程原版 | 当前 Responses API 实现 |
|---|---|
| `client.messages.create(...)` | `cilent.responses.create(...)` |
| `system` / `messages` / `max_tokens` | `instructions` / `input` / `max_output_tokens` |
| `response.content` | `response.output` |
| `tool_use` | `function_call` |
| `block.input` | `json.loads(block.arguments)` |
| `block.id` | `block.call_id` |
| `tool_result` / `tool_use_id` / `content` | `function_call_output` / `call_id` / `output` |
| assistant 消息包装模型输出 | `inputs.extend(response.output)` |
| user 消息包装工具结果 | `inputs.extend(results)` |

## run_shell 的执行范围

关键调用是：

```python
r = subprocess.run(
    command,
    shell=True,
    cwd=os.getcwd(),
    capture_output=True,
    text=True,
    errors="replace",
    check=False,
    timeout=120,
)
```

| 参数 | 作用 |
|---|---|
| `shell=True` | 交给 Shell 解析，支持管道、重定向等语法；macOS 默认通常使用 `/bin/sh` |
| `cwd=os.getcwd()` | 从 Python 当前工作目录开始执行，不一定是代码文件所在目录 |
| `capture_output=True` | 收集标准输出和标准错误 |
| `text=True`、`errors="replace"` | 输出转为字符串，解码失败的字符用替代字符处理 |
| `check=False` | 非零退出码不会自动触发 `CalledProcessError` |
| `timeout=120` | 最多等待 120 秒，超时进入异常分支 |

执行完成后，代码合并 `stdout` 和 `stderr`，去除首尾空白，最多返回 50,000 个字符；没有输出返回 `"no output"`。两路输出拼接后不保留原本交错的时间顺序。终端只打印前 200 个字符，回传模型的仍是函数返回的完整字符串。

`cwd` 不是目录隔离：命令仍能用 `../`、绝对路径或 `cd .. && ...` 访问其他有权限的位置。一次调用中的 `cd` 不会改变下一次调用的起点，因为每次启动新的 Shell。

当前黑名单只是字符串匹配，不是沙箱。工具继承 Python 进程的权限，也没有提供交互式终端。当前代码不根据退出码标记成功或失败；API 请求、参数解析、三轮耗尽产生的异常也没有在入口处统一捕获。

## 运行和观察

从包含 `pyproject.toml` 的项目目录运行：

```sh
cd learn_claude_code
uv sync
uv run python src/learn_claude_code/s01_agent_loop/code.py
```

预先配置 `OPENAI_API_KEY` 与 `MODEL_ID`，模型必须支持 Responses API 和函数调用。可以在该项目目录准备 `.env`：

```dotenv
OPENAI_API_KEY=你的密钥
MODEL_ID=你有权限使用的模型ID
```

这里直接运行脚本，项目的命令行入口目前没有连接到这个脚本。启动目录就是 Shell 的执行起点，尝试写文件操作时应选用可丢弃的测试目录。

可以依次尝试“回复你好，不调用工具”“执行 pwd，告诉我当前目录”“列出当前目录的 Python 文件”。观察是否产生 `function_call`、日志里的参数和输出，以及最终回答如何使用工具结果。

`readline` 段只用于命令行编辑体验与兼容处理；没有安装时跳过，不参与模型与工具之间的数据流。

## 继续学习：为什么拆出专用工具

Shell 可以读写文件、搜索和执行脚本，但复杂内容需要处理命令拼接与转义。下一步可将常用文件操作封装为独立工具，明确路径和内容参数。

增加工具后，需要按工具名称分发到对应函数；是否并发则由执行程序决定。读取两个独立文件通常可以并发，修改同一个文件会产生冲突，需要先看结果再决定的操作应分轮执行。工具数量、一次回复的调用数量和并发执行是不同的问题。
