# s02：工具分发与文件操作

## 核心流程

专用文件工具让模型直接提交结构化参数，减少 shell 命令的引号和转义错误，也方便后续按工具、路径控制权限。

```text
用户请求 → 模型返回工具名和参数 → 查表执行 → 回传结果 → 继续调用或回答
```

新增工具：实现函数 → 在 `TOOLS` 声明 → 在 `TOOL_HANDLES` 注册，循环无需增加分支。当前五个工具均已接入；`safe_path` 是内部辅助函数。

```python
handler = TOOL_HANDLES.get(block.name)
arguments = json.loads(block.arguments)
output = handler(**arguments) if handler else f"Unknown handler: {block.name}"
```

- 分发表保存函数对象；工具名必须与字典键一致，参数名必须匹配函数签名。
- `**arguments` 将字典展开为关键字参数：`{"path": "a.md"}` → `run_read(path="a.md")`。
- 先 `inputs.extend(response.output)` 保留模型输出，再追加每个调用的结果。一轮多个调用按顺序执行，不是并发。
- 四个文件操作函数将异常转成 `Error: ...` 字符串，供模型继续处理；这属于正常返回，不是向外抛异常。

## 官方与本地的接口差异

| 项目 | 官方 Anthropic | 本地 OpenAI Responses |
|---|---|---|
| 请求 | `messages.create()` | `responses.create()` |
| 提示词 / 历史 | `system` / `messages` | `instructions` / `input` |
| 工具声明 | `input_schema` | `type="function"` + `parameters` |
| 调用 | `response.content` 中的 `tool_use` | `response.output` 中的 `function_call` |
| 参数 | `block.input` 字典 | `json.loads(block.arguments)` |
| 结果 | `tool_result` + `tool_use_id=block.id` | `function_call_output` + `call_id=block.call_id` |
| shell 名称 | `bash` | `shell` |

本地结果直接加入 `inputs`，不套用官方的 `role="user"` 包装；无工具调用时返回 `response.output_text`。

官方持续循环，本地 `range(3)` 最多请求模型 3 次，包含最终回答。若第 3 次仍调用工具，执行后会抛异常，模型无法继续读取结果并回答。

## 五个函数的关键细节

### `safe_path(p)`：规范化并检查路径

```python
path = (WORKDIR / p).resolve()
if not path.is_relative_to(WORKDIR):
    raise ValueError(f"Path escapes workspace: {p}")
```

- `/` 拼接路径；`p` 为绝对路径时以它为准。`resolve()` 消除 `..`、解析符号链接并得到绝对路径，默认不要求目标存在。
- `is_relative_to()` 按路径层级判断是否等于工作目录或位于其内部，不是字符串前缀匹配，也不会自行解析符号链接。
- `WORKDIR = Path.cwd()` 是启动目录，不一定是源码目录；显式配置时应使用规范化的绝对路径。
- 例如工作目录 `/project` 中的 `../secret.txt` 会被拒绝。检查后链接仍可能被更换，因此不是系统级隔离；shell 的 `cwd` 也只设置起始目录。

### `run_read(path, limit=None)`：读取文本

```python
lines = safe_path(path).read_text(encoding="utf-8").splitlines()
if limit and limit < len(lines):
    lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
return "\n".join(lines)
```

- 数据变化：`Path → 字符串 → 行列表 → 字符串`。`splitlines()` 去掉换行符，`join()` 重新连接，不保留原始换行格式和末尾换行。
- 切片取前几行，再拼接提示列表；右侧计算完才赋值，所以提示使用原总行数。
- `limit: int | None` 不会自动校验输入；`None`、`0` 均不限制，负数会产生不准确的省略提示。
- 先完整读取再截取，限制的是返回行数，不是读取量。

### `run_write(path, content)`：创建或覆盖

```python
file_path.parent.mkdir(parents=True, exist_ok=True)
file_path.write_text(content, encoding="utf-8")
```

- `.parent` 是父目录；`parents=True` 创建缺少的各层目录，`exist_ok=True` 允许目录已存在，不忽略其他错误。
- `write_text()` 自动打开、写入、关闭；文件不存在则创建，存在则覆盖全部内容，不是追加。
- 提示中的 `len(content)` 是字符串长度，不是 UTF-8 字节数：`"你好"` 长度为 2，编码后为 6 字节，当前 `bytes` 单位不准确。

### `run_edit(path, old_text, new_text)`：替换第一处原文

- 读取整个文件，找不到 `old_text` 则返回错误；匹配要求空格、大小写、换行一致，可以匹配多行代码。
- `text.replace(old_text, new_text, 1)` 返回新字符串，只替换第一处，再将整个新文本覆盖写回。
- 不检查匹配唯一性，应提供足够上下文。例如 `"cat cat"` 替换一次后是 `"dog cat"`。
- 空的 `old_text` 未被禁止，会在文件开头插入新文本。

### `run_glob(pattern)`：按路径名称查找

```python
matches = sorted({
    match for match in g.glob(pattern, root_dir=WORKDIR, recursive=True)
    if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
})
```

- `import glob as g` 是模块别名；glob 匹配路径名称，不查文件内容，也不是正则表达式。
- `root_dir` 设置相对模式的搜索起点；`recursive=True` 开启 `**` 的递归能力，普通 `*` 不会自动递归。
- `*.py` 查当前层，`src/*.py` 查 `src` 这一层，`**/*.py` 查当前及各层子目录。
- 集合推导式负责筛选、去重，`sorted()` 得到有序列表；过滤解析后越界的结果，不限制搜索过程的访问范围。
- 完成搜索和排序后才取前 200 项；超出追加提示，无结果返回 `(no matches)`。

## 运行与观察

从含 `pyproject.toml` 的项目根目录运行，配置 `.env` 中的 `OPENAI_API_KEY` 和 `MODEL_ID`：

```sh
uv run python src/learn_claude_code/s02_tool_use/code.py
```

尝试创建文件并读回、替换原文、查找 `**/*.py`。终端仅展示结果前 200 字符，模型收到的仍是完整 `output`。写出人物简介不代表联网核实：本课未注册搜索工具，是否通过 shell 联网需看调用记录。

参考：[官方中文说明](https://github.com/shareAI-lab/learn-claude-code/blob/main/s02_tool_use/README.zh.md) · [官方章节源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s02_tool_use/code.py)（与 `agents/` 下同名版本不同）。
