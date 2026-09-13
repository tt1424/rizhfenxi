# Plan 日志查询器

本地自用工具。后端复用 `stdout-log-analysis` 的查询客户端，凭据只在本机服务端读取，不会发送到浏览器页面。

## 启动

在仓库根目录执行：

```powershell
.\tmp\plan-log-viewer\start.ps1
```

然后打开 <http://127.0.0.1:8765/>。

如果本机 Python 环境不是仓库 `.venv`，也可以执行：

```powershell
python .\tmp\plan-log-viewer\server.py
```

## 查询

- 输入完整日志 ID 和北京时间日期；日期会自动转换为当天 `00:00` 到次日 `00:00`。
- `自动` 同时查 `naviag-dev`、`naviag`、`d-agent`、`llm-master-agent`，适合 Planner 分类可能落在上游的情况。
- D-agent 使用正则项目条件，同时覆盖 `d-agent`、`d-agent-prod` 和 `d-agent-test`。
- 三段式日志 ID 会按第一段根 ID 查询关联链路。
- `Plan=multi_poi_navi` 表示多途经点/多 POI 导航 Plan；`isMultiIntent=true` 表示同一轮有多个独立意图，两者分别展示。
- 历史 `dialogHistory` 里的旧 Plan 只作为低优先级证据，不会覆盖当前请求的 Planner 结果。
- 页面不展示分类证据明细；只有当前 Plan 为 `simple_navi` 时，才会追加显示 `naviag` / `naviag-dev` 日志。
- 所有能提取到历史轮次的 Plan 都会显示 `上轮对话`；`simple_navi` 额外按 `NaviInfo → 原始 Input → 关键流程 → 最终输出` 展示，全部原始 Navi 日志默认折叠在最后。
- 链路总览只使用 `naviag` / `naviag-dev` 日志展示 Navi 内部实际链路，不展示 D-agent 的 Planner 或子 Agent 下发过程。
