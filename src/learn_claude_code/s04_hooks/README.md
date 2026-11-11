s03增加了Agent的权限检查了，但后期每次新增加一个检查，比如“记录每次shell调用“、“操作后自动git add“，都要修改agent_loop函数
循环很快就会变成这样：

def agent_loop(messages):
    while True:
        # ... LLM call ...
        for block in response.content:
            if block.type != "tool_use":
                continue
            log_to_file(block)          # 加一行
            check_permission(block)     # 加一行
            notify_slack(block)         # 又加一行
            output = execute(block)
            auto_git_add(block)         # 再加一行
            # ... 很快循环就认不出来了

你想扩展的是 Agent 的行为，但你改的却是循环本身。循环应该是一个稳定的核心，扩展应该挂在外面。

总结：扩展逻辑定义在循环外；触发入口放在循环内特定位置