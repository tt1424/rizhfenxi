# Special Agent Trace Rule

`llm-master-agent` 和 `lyra-auto-agent` 的查询不能只停留在 recordId。

标准链路：

```text
recordId
  -> llm-master-agent: 查询包含 recordId 的 LLM.UP.REQUEST 日志
  -> lyra-auto-agent: 查询包含 recordId 的 lyra-auto-agent 日志
  -> 从命中的 agent 日志括号或字段中提取 traceId
  -> 用 traceId 查询完整链路日志
  -> 将 traceId 查询命中的日志单独保存到 /Volumes/D/logs/stdout-trace-<traceId>-<timestamp>.jsonl
  -> 基于完整链路拆解延迟
```

分析时优先关注：

- `LLM.UP.REQUEST`
- `LLM.UP.RESPONSE`
- `lyra-auto-agent` 的 recordId 命中日志
- planner / master 编排日志
- skill / sub-agent 调用日志
- 模型上游请求与响应日志
- tool 调用日志
- error / warn / exception / timeout
