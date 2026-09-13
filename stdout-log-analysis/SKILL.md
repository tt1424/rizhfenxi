---
name: stdout-log-analysis
description: Use when the user asks to query AISpeech vLogs Stdout or Syslog logs, analyze recordId or traceId, download logs, locate llm-master-agent or lyra-auto-agent issues, inspect LLM.UP.REQUEST, split latency, troubleshoot slow response, failed request, timeout, PMS/agent bug, or full链路日志分析.
---

# Stdout Log Analysis

## 使用场景

当用户要求根据 `recordId`、`traceId`、服务名、报错现象查询 Stdout/Syslog 日志、下载日志、分析线上问题、拆解链路耗时或定位 `llm-master-agent` / `lyra-auto-agent` 问题时，使用本 skill。

## 安全要求

- 不要把账号、密码、token 写进回答、日志文件正文或 Git 提交。
- 凭据从 `~/.codex/secrets/stdout-log.env` 读取。
- 如果需要展示查询命令，只展示脱敏形式。
- 下载日志默认保存到 `/Volumes/D/logs/`。

## 服务名定位

- **Navi Agent 主体服务的 Stdout `pod_project` 是 `naviag`**。用户说查 Navi Agent、navi agent、navi-agent、navigation agent、导航子 Agent、`sub_agent.navi_agent` 或“我的服务”时，优先使用 `--service naviag` 查询。
- **默认 Stdout 项目索引集合是 `d-agent`、`llm-master-agent`、`naviag`**。只要查询 Stdout 日志，查询表达式必须在 `recordId`、`traceId` 或关键词之外带 `pod_project` 索引；未能判断单个服务时使用 `{pod_project in ("d-agent", "llm-master-agent", "naviag")}`，不要裸扫全局日志。
- 不要把 Navi Agent 主体日志误查成 `navi-agent`、`navigation-agent` 或 `d-agent`；这些名称通常不是当前 Navi Agent 的 Stdout 服务名。
- `lyra-auto-agent` 是上游编排/TSM 侧日志，可用于看上游正式 WebSocket 请求、`dialogHistory` 和上游下发的 `functions`；但定位 Navi Agent 内部推理、路由、MCP、Travel、nativeapi 或 `sub_agent.navi_agent.*` 日志时，应回到 `naviag`。

## 输入字段

- `recordId`: 用户提供的请求记录 ID。普通查询和 `llm-master-agent` 查询都优先使用。
- `traceId`: 可选。如果用户直接提供 traceId，可以直接查询完整链路。
- `service`: 可选。用户未指定时不要先全局查；Stdout 默认使用 `pod_project in ("d-agent", "llm-master-agent", "naviag")` 索引集合。如果问题包含 `llm-master-agent` 或 `lyra-auto-agent`，按特殊链路处理。
- `timeRange`: 可选。默认最近 72 小时；用户给出时间时必须使用用户时间。查询窗口不要超过 75 小时。
- `question`: 用户要分析的问题，比如延迟、失败、无响应、模型调用慢、下游异常。
- `download`: 用户要求下载/导出/保存日志时开启。
- `logType`: 可选，默认 `stdout`；查 Syslog 时使用 `syslog`。

## 查询策略

### 普通 recordId 查询

1. 使用 `recordId` 作为关键词查询 stdout 日志，同时加 `pod_project` 索引。
2. 如果用户提供 `service`，映射为 Stdout 索引 `pod_project`。
3. 如果问题主体是 Navi Agent / 导航子 Agent / `sub_agent.navi_agent`，或用户说“我的服务”，默认 `service=naviag`，先查 `{pod_project="naviag"}`。
4. 如果无法判断单个服务，默认查 `{pod_project in ("d-agent", "llm-master-agent", "naviag")}`，再叠加 `recordId` / `traceId` / 关键词。
5. 只有用户明确要求“全局查 / 放宽全局 / 跨项目兜底”时，才去掉 `pod_project` 做全局查询。
6. 按时间排序、去重、提取异常、耗时和 request/response 片段。
7. 根据用户问题输出结论。

### Navi Agent 主体日志查询

```bash
python3 ~/.codex/skills/stdout-log-analysis/scripts/stdout_client.py analyze \
  --record-id '<recordId>' \
  --service naviag \
  --question '<用户问题>'
```

### 特殊 agent trace 链路查询

触发条件：

- 用户明确提到 `llm-master-agent`。
- 用户明确提到 `lyra-auto-agent`。
- 或用户问题包含 `master agent`、`LLM.UP.REQUEST`、完整链路、traceId。
- 或普通 recordId 日志中出现 `LLM.UP.REQUEST`。

处理步骤：

1. 如果是 `llm-master-agent`，使用 `{pod_project="llm-master-agent"} AND "LLM.UP.REQUEST" AND "<recordId>" | fields _time, pod_name, pod_project, k8scluster, _msg` 查询日志。
2. 如果是 `lyra-auto-agent`，使用 `{pod_project="lyra-auto-agent"} AND "<recordId>" | fields _time, pod_name, pod_project, k8scluster, _msg` 查询日志。
3. 从命中的 agent 日志括号或字段中提取 `traceId`。
4. 使用 `traceId` 二次查询完整链路日志；仍然必须带 `pod_project` 索引，无法判断单服务时使用默认集合 `{pod_project in ("d-agent", "llm-master-agent", "naviag")}`。
5. 将 `traceId` 二次查询命中的日志单独保存为 `/Volumes/D/logs/stdout-trace-<traceId>-<timestamp>.jsonl`，并在脚本 JSON 结果中返回 `traceLogFile`。
6. 按时间线拆解完整链路耗时。
7. 输出结论、关键证据、耗时分布、瓶颈点和建议。

## traceId 提取优先级

按以下顺序提取：

1. `traceId=...`、`trace_id=...`、`trace-id=...`
2. JSON 字段 `"traceId"`、`"trace_id"`、`"trace-id"`
3. 日志括号中的 16-64 位十六进制或冒号分隔链路 ID
4. 如果多个候选值同时存在，优先选择和 `LLM.UP.REQUEST` 同一日志行最近的值

## 延迟分析方法

对完整日志做时间线分析：

1. 找到链路第一条和最后一条日志，计算总耗时。
2. 提取关键阶段：
   - 请求进入 `llm-master-agent`
   - `LLM.UP.REQUEST`
   - planner / master 编排
   - skill / sub-agent 调用
   - 模型请求发起
   - 模型响应返回
   - 下游工具/API 调用
   - 最终响应生成
3. 计算相邻关键事件的间隔。
4. 找出最大耗时区间。
5. 区分事实和推断；没有日志证据时必须说明证据不足。

## 标准输出格式

```markdown
## 结论
一句话说明根因或当前最可能瓶颈，给出置信度。

## 查询信息
- recordId:
- traceId:
- 服务:
- 时间范围:
- 命中日志数:
- 日志文件:
- traceId 日志文件:

## 延迟拆解
| 阶段 | 开始时间 | 结束时间 | 耗时 | 证据 |
|---|---:|---:|---:|---|

## 关键证据
| 时间 | 服务 | 级别 | 日志摘要 | 说明 |
|---|---|---|---|---|

## 链路还原
1. ...
2. ...

## 根因判断
- 直接原因:
- 深层原因:
- 置信度:

## 建议动作
- 立即处理:
- 后续优化:
- 需要补充:
```

## 工具脚本

优先使用：

```bash
python3 ~/.codex/skills/stdout-log-analysis/scripts/stdout_client.py analyze \
  --record-id '<recordId>' \
  --question '<用户问题>' \
  --download
```

默认会生成 `{pod_project in ("d-agent", "llm-master-agent", "naviag")}` 索引查询；明确查 master 时再加 `--service llm-master-agent`。

分析 `lyra-auto-agent` 时：

```bash
python3 ~/.codex/skills/stdout-log-analysis/scripts/stdout_client.py analyze \
  --record-id '<recordId>' \
  --service lyra-auto-agent \
  --question '<用户问题>' \
  --download
```

如果只想下载：

```bash
python3 ~/.codex/skills/stdout-log-analysis/scripts/stdout_client.py download \
  --record-id '<recordId>' \
  --service llm-master-agent
```

查询 Syslog 时：

```bash
python3 ~/.codex/skills/stdout-log-analysis/scripts/stdout_client.py analyze \
  --log-type syslog \
  --record-id '<关键词或recordId>' \
  --service dds \
  --cluster d1-prod \
  --start 5m \
  --end now
```

## 失败处理

- `recordId` 查不到：说明查询范围、环境、时间范围可能不对，要求补充时间。
- 找不到 `LLM.UP.REQUEST`：说明请求未进入 `llm-master-agent` 或日志关键词不同，改用 recordId 全局查询。
- `lyra-auto-agent` recordId 命中但找不到 traceId：输出 lyra 命中日志片段并说明提取失败。
- 找不到 traceId：输出 `LLM.UP.REQUEST` 命中日志片段并说明提取失败。
- traceId 二次查询无结果：输出 traceId 和首次命中证据，建议扩大时间范围。
- 接口返回 HTML 导航页或 `unsupported path requested`：说明当前 API 路径或查询参数映射未确认，需要根据 wiki 补全 `STDOUT_LOG_BASE_URL` 和查询参数。
- 如果 `{k8scluster="d1-prod"}` 无结果，要放宽到 `{pod_project="llm-master-agent"}`，因为同一服务可能在 `d1-beta` 等集群。
