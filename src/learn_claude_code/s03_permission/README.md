# s03：执行前的权限判断

在 s02 的工具分发前加入 `check_permission()`，由程序决定拒绝、询问或执行。模型负责提出工具调用，审批由程序处理，不依赖模型自己询问用户。

## 三道 Gate

| Gate | 判断内容 | 命中后 |
|---|---|---|
| 1：硬拒绝 | shell 是否包含 `DENY_LIST` 中的片段 | 直接拒绝，不允许用户批准 |
| 2：规则匹配 | 工具参数是否触发 `PERMISSION_RULES` | 返回原因，进入 Gate 3 |
| 3：用户确认 | 用户是否输入 `y` / `yes` | 同意才执行，其他输入均拒绝 |

```text
工具调用 → Gate 1 硬拒绝检查 → Gate 2 规则检查
              ↓ 命中              ├─ 未命中 → 执行
             拒绝                 └─ 命中 → Gate 3 → 同意执行 / 拒绝
```

### 为什么 Gate 1 不够，还需要 shell rule？

两者表达不同的策略：硬拒绝处理“本程序始终禁止”，规则处理“可能合理，但需要用户决定”。Gate 1 没命中只表示没有触发硬禁令，不等于操作已经安全。

| 调用示例 | Gate 1 | Gate 2 / 3 |
|---|---|---|
| `shell(command="sudo ls")` | 命中，直接拒绝 | 不再进入 |
| `shell(command="rm test.txt")` | 未命中 | 删除可能是正常清理，先询问 |
| `shell(command="chmod 777 test.txt")` | 未命中 | 权限修改有风险，先询问 |
| `shell(command="ls")` | 未命中 | 未命中，直接执行 |
| `write_file(path="../outside.txt", ...)` | 不适用 | 路径越界，先询问 |

如果只有 Gate 1，`rm test.txt` 等未列入硬黑名单的操作就会直接执行；如果把所有删除都列入硬黑名单，用户也无法批准正常清理。增加 shell rule 是为了区分禁止与需确认，并不是让用户覆盖硬拒绝。

## 规则如何执行

每条规则保存 `tools`（适用工具）、`check`（检查函数）、`message`（确认原因）。`lambda` 保存的是函数对象，检查时才调用：

```python
if tool_name in rule["tools"] and rule["check"](args):
    return rule["message"]
```

`and` 短路：工具名不匹配就不调用检查函数；返回第一条命中原因，全部未命中返回 `None`。这里 `check=True` 表示需要确认，不是允许执行。

- 文件规则：对 `read_file`、`write_file`、`edit_file` 的 `path` 规范化后检查是否越界。s03 移除了这些函数内部的 `safe_path` 硬拒绝，因此用户批准后可以访问工作区外。
- shell 规则：读取 `command`，正则识别命令位置上的 `rm` / `del`，再用 `any(kw in command for kw in keywords)` 补充关键词检查；两者任一命中即询问。`glob` 仍在函数内部过滤越界结果。
- 正则中的 `(?i)` 忽略大小写，`(?:...)` 用于分组，`\s*` 允许空白，`(?=...)` 检查后续边界而不消耗字符；`search()` 返回匹配对象或 `None`，`bool()` 转成判断结果。

这些检查是教学用的文本匹配，不是完整 shell 解析或系统级隔离，会误报和漏报。未命中不代表已证明安全。

## 本地 OpenAI 实现

官方使用 Anthropic 的 `block.input` 字典；本地从 `json.loads(block.arguments)` 得到参数，工具名称使用 `shell` 而非 `bash`。

拒绝后也必须回传结果，让模型知道调用未执行：

```python
{
    "type": "function_call_output",
    "call_id": block.call_id,
    "output": "Permission denied.",
}
```

随后 `continue` 跳过当前工具；允许时才调用 handler。`inputs.extend(response.output)` 保留模型调用，`inputs.extend(results)` 追加对应结果，不照搬官方 `tool_result` 的消息格式。

当前本地最多请求模型 5 次，官方持续循环到无工具调用；本地异常提示仍写 3 次，应同步。当前 shell 关键词写的是 `"rm"`，比官方 `"rm "` 更宽，会误报 `mkdir farm` 等命令。


来源：[官方中文 README](https://github.com/shareAI-lab/learn-claude-code/blob/main/s03_permission/README.zh.md) · [官方章节源码](https://github.com/shareAI-lab/learn-claude-code/blob/main/s03_permission/code.py)。下一课 s04 将通过 Hooks 组织执行前后的扩展逻辑。
