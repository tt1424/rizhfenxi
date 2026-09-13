from __future__ import annotations

import json
import re
from typing import Any, Iterable


HEADER_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ - .*? - (?:INFO|WARNING|ERROR|DEBUG) - "
)
RECORD_TAIL_RE = re.compile(r"\s*\[recordid=.*?\]\s*$", re.S | re.I)
USER_INPUT_RE = re.compile(r"用户输入:\s*(.*?)(?:\s*\[recordid=|$)", re.S)


EVENT_RULES = (
    ("error", "异常 / 警告", re.compile(r"\bERROR\b|\bWARNING\b|异常|失败|Traceback|VIOLATION|timeout", re.I)),
    ("entry", "接收请求", re.compile(r"收到导航请求")),
    ("route", "前置判定", re.compile(r"early streaming|垫句判定", re.I)),
    ("route", "入口函数", re.compile(r"请求函数:")),
    ("route", "语义路径", re.compile(r"semantic[_ ]loop|快速旁路|快路径", re.I)),
    ("route", "Stage1 分类", re.compile(r"stage1|意图分类", re.I)),
    ("route", "Travel 路由", re.compile(r"路由到 TravelSkillAgent|TravelAdapter.*开始处理|创建 TravelSkillAgent", re.I)),
    ("state", "状态 / 上下文", re.compile(r"待处理的交互|selection_context|pending_interaction|记忆消费|上下文恢复", re.I)),
    ("tool", "工具调用", re.compile(r"执行工具调用|MCP调用|发送 .* 到 MCP|MapApiTrigger.*参数|FakeWebSocket.*MCP调用", re.I)),
    ("tool", "工具结果", re.compile(r"MCP返回|MCP 返回|解析结果数量|搜索结果|候选数量", re.I)),
    ("output", "输出生成", re.compile(r"开始流式NLG|NLG最终输出|管道完成|响应已发送", re.I)),
)

DEEP_FUNCTIONS = {"mapdeep", "map_navigation_deep", "deep", "dp"}


def _first_matching_entry(entries: list[dict[str, Any]], *patterns: str) -> dict[str, Any] | None:
    compiled = [re.compile(pattern, re.I) for pattern in patterns]
    return next(
        (entry for entry in entries if any(pattern.search(_message(entry)) for pattern in compiled)),
        None,
    )


def _last_matching_entry(entries: list[dict[str, Any]], *patterns: str) -> dict[str, Any] | None:
    compiled = [re.compile(pattern, re.I) for pattern in patterns]
    return next(
        (entry for entry in reversed(entries) if any(pattern.search(_message(entry)) for pattern in compiled)),
        None,
    )


def _route_node(
    entry: dict[str, Any] | None,
    key: str,
    title: str,
    detail: str,
    kind: str = "route",
) -> dict[str, str]:
    return {
        "key": key,
        "title": title,
        "detail": detail,
        "kind": kind,
        "timestamp": str((entry or {}).get("_time") or (entry or {}).get("time") or ""),
        "service": str((entry or {}).get("pod_project") or (entry or {}).get("app_name") or ""),
        "evidence": _clean_message(_message(entry), 520) if entry else "",
    }


def _extract_route_summary(
    entries: list[dict[str, Any]], input_info: dict[str, Any] | None, plan_type: str | None = None
) -> dict[str, Any]:
    nodes: list[dict[str, str]] = []
    function_name = str((input_info or {}).get("function") or "").strip()
    normalized_function = function_name.lower()
    upstream_result = str((input_info or {}).get("upstreamResult") or "").strip().lower()
    request_entry = _first_matching_entry(entries, r"收到原始input:", r"上游请求内容:", r"LLM.UP.REQUEST:")
    if not function_name and upstream_result == "deep":
        nodes.append(_route_node(request_entry, "upstream", "上游 Deep", "res=deep"))
    elif not function_name:
        nodes.append(_route_node(request_entry, "upstream", "上游未传 FC", "functions.request 为空"))
    elif normalized_function in DEEP_FUNCTIONS:
        nodes.append(_route_node(request_entry, "upstream", "上游 Deep", function_name))
    else:
        nodes.append(_route_node(request_entry, "upstream", "上游 FC", function_name))

    planner_entry = _first_matching_entry(entries, r"KEYLOG\.PLANNER\.RESULT")
    downstream_entry = _first_matching_entry(entries, r"LLM\.DOWN\.REQUEST:\s*sub_agent=")
    if plan_type:
        nodes.append(
            _route_node(
                planner_entry or downstream_entry,
                "planner",
                "Planner 分类",
                plan_type,
            )
        )
    if downstream_entry:
        match = re.search(r"LLM\.DOWN\.REQUEST:\s*sub_agent=([A-Za-z0-9_-]+)", _message(downstream_entry), re.I)
        if match:
            nodes.append(
                _route_node(
                    downstream_entry,
                    "downstream",
                    "下发子 Agent",
                    match.group(1),
                )
            )

    null_to_deep = _first_matching_entry(entries, r"function.*null.*mapDeep")
    if null_to_deep:
        nodes.append(_route_node(null_to_deep, "null_to_deep", "补全为 Deep", "空 Function 转为 mapDeep"))

    fixed_fc = _first_matching_entry(entries, r"固定 FC 命中|project_fixed_function")
    if fixed_fc:
        nodes.append(_route_node(fixed_fc, "fixed_fc", "项目固定 FC", "跳过 Deep 和语义 Loop"))

    loop_entry = _last_matching_entry(
        entries,
        r"semantic_loop branch=",
        r"semantic_loop mode=",
        r"semantic_loop context_router",
        r"semantic_loop eligibility",
        r"semantic_loop 快速旁路",
        r"semantic_loop raw_input_rule=",
    )
    if loop_entry:
        loop_text = _message(loop_entry)
        if re.search(r"快速旁路|bypass", loop_text, re.I):
            loop_title = "Semantic Loop 旁路"
        elif re.search(r"handled|action=", loop_text, re.I):
            loop_title = "Semantic Loop 推理"
        else:
            loop_title = "Semantic Loop 判定"
        nodes.append(_route_node(loop_entry, "semantic_loop", loop_title, _clean_message(loop_text, 260)))

    rewrite_entry = _last_matching_entry(
        entries,
        r"KEYLOG\.NAVI\.DEEP_REWRITE\.FUNCTION_CALL",
        r"KEYLOG\.NAVI\.DEEP_REWRITE\.STANDARD_QUERY",
        r"VLA deep 专用改写命中",
        r"\[别名改写\]",
        r"\[记忆改写\]",
    )
    if rewrite_entry:
        rewrite_text = _message(rewrite_entry)
        if "FUNCTION_CALL" in rewrite_text:
            rewrite_title = "Deep 改写并拆解 FC"
        elif "VLA" in rewrite_text:
            rewrite_title = "项目标准话术改写"
        elif "别名改写" in rewrite_text:
            rewrite_title = "别名改写"
        elif "记忆改写" in rewrite_text:
            rewrite_title = "记忆改写"
        else:
            rewrite_title = "Deep 标准改写"
        nodes.append(_route_node(rewrite_entry, "rewrite", rewrite_title, _clean_message(rewrite_text, 300)))

    native_entry = _first_matching_entry(entries, r"nativeapi.*handled=true", r"NativeAPI.*命中")
    if native_entry:
        nodes.append(_route_node(native_entry, "nativeapi", "NativeAPI 直达", "由 NativeAPI 完成本轮"))

    travel_entry = _first_matching_entry(
        entries,
        r"路由到 TravelSkillAgent",
        r"TravelAdapter.*开始处理",
        r"TravelSkillAgent.*开始处理请求",
    )
    if travel_entry:
        nodes.append(_route_node(travel_entry, "travel", "进入 Travel", _clean_message(_message(travel_entry), 260)))

    source_rules = (
        ("complexity", "复杂 POI 云端解析", (r"命中 complexityPoi 快路径", r"complexityPoi火山")),
        ("volc", "火山候选搜索", (r"火山候选流式搜索", r"火山流式验证")),
        ("websearch", "WebSearch", (r"WebSearch预发射", r"火山WebSearch", r"web_search")),
        ("amap", "高德地图", (r"高德", r"\bAMap\b", r"maps_(?:text|around|geo|route)_search")),
        ("iqs", "IQS", (r"\bIQS\b",)),
        ("rag", "RAG", (r"RAG 前置命中", r"RAG前置命中", r"travel_rag")),
    )
    data_sources: list[dict[str, str]] = []
    for source_key, source_title, patterns in source_rules:
        source_entry = _first_matching_entry(entries, *patterns)
        if source_entry:
            data_sources.append(
                {
                    "key": source_key,
                    "title": source_title,
                    "timestamp": str(source_entry.get("_time") or source_entry.get("time") or ""),
                    "evidence": _clean_message(_message(source_entry), 420),
                }
            )
    if data_sources:
        first_source = min(data_sources, key=lambda item: item["timestamp"] or "z")
        source_entry = _first_matching_entry(entries, re.escape(first_source["title"]))
        nodes.append(
            _route_node(
                source_entry,
                "data_source",
                "数据检索",
                " + ".join(source["title"] for source in data_sources),
                "source",
            )
        )

    tool_entry = _last_matching_entry(
        entries,
        r"执行工具调用:",
        r"发送 .* 到 MCP",
        r"MCP调用:",
        r"navigateToAddress 参数",
        r"addWaypoint 参数",
    )
    if tool_entry:
        nodes.append(_route_node(tool_entry, "execution", "执行动作", _clean_message(_message(tool_entry), 320), "tool"))

    error_entry = _first_matching_entry(entries, r"\bERROR\b", r"Traceback", r"处理异常", r"调用失败")
    if error_entry:
        nodes.append(_route_node(error_entry, "error", "链路异常", _clean_message(_message(error_entry), 320), "error"))

    return {
        "headline": " → ".join(node["title"] for node in nodes if node["key"] != "error"),
        "nodes": nodes,
        "dataSources": data_sources,
        "upstreamType": "deep" if normalized_function in DEEP_FUNCTIONS or upstream_result == "deep" else "fc" if function_name else "none",
        "upstreamFunction": function_name or None,
    }


def _message(entry: dict[str, Any]) -> str:
    return str(entry.get("_msg") or entry.get("msg") or entry.get("message") or "")


def _json_after(text: str, marker: str) -> Any | None:
    marker_index = text.find(marker)
    if marker_index < 0:
        return None
    start = text.find("{", marker_index + len(marker))
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
        return value
    except json.JSONDecodeError:
        return None


def _walk(value: Any, path: tuple[str, ...] = ()):
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, path + (str(key),))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, path + (str(index),))


def _find_naviinfo(payloads: Iterable[Any]) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    order = 0
    for payload in payloads:
        for path, value in _walk(payload):
            if not path or path[-1].lower() != "naviinfo" or not isinstance(value, dict):
                continue
            score = 0
            lowered = {part.lower() for part in path}
            if "dialoghistory" in lowered:
                score -= 100
            if "settings" in lowered:
                score += 20
            if "system" in lowered or "context" in lowered:
                score += 10
            candidates.append((score, -order, value))
            order += 1
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _record_timestamp(record_id: str | None) -> int | None:
    if not record_id:
        return None
    value_text = str(record_id).strip()
    timestamp_text = value_text[-10:]
    if not re.fullmatch(r"\d{10}", timestamp_text):
        return None
    return int(timestamp_text)


def _history_timestamp(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number // 1000 if number > 10_000_000_000 else number


def _history_items(payloads: Iterable[Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for payload in payloads:
        for path, value in _walk(payload):
            if not path or path[-1] != "dialogHistory" or not isinstance(value, list):
                continue
            candidates.extend(item for item in value if isinstance(item, dict))
    return candidates


def _summarize_history_turn(turn: dict[str, Any]) -> dict[str, Any]:
    output = turn.get("output")
    output_dict = output if isinstance(output, dict) else {}
    widget = output_dict.get("widget")
    widget_dict = widget if isinstance(widget, dict) else {}
    speak = output_dict.get("speak")
    speak_dict = speak if isinstance(speak, dict) else {}
    functions = turn.get("functions")
    functions_dict = functions if isinstance(functions, dict) else {}
    request = functions_dict.get("request")
    request_dict = request if isinstance(request, dict) else {}
    content = widget_dict.get("content")
    candidate_count = len(content) if isinstance(content, list) else None
    display_text = widget_dict.get("displayText")
    if not isinstance(display_text, str) or not display_text.strip():
        display_text = speak_dict.get("text")
    if not isinstance(display_text, str) or not display_text.strip():
        display_text = output if isinstance(output, str) else ""
    needs_user_input = widget_dict.get("needsUserInput")
    if needs_user_input is None:
        needs_user_input = bool(widget_dict.get("maNeedUserInputDomain")) or None
    function_name = str(request_dict.get("name") or "")
    return {
        "recordId": str(turn.get("recordId") or ""),
        "timestamp": turn.get("timestamp"),
        "input": str(turn.get("input") or ""),
        "displayText": str(display_text or ""),
        "plan": str(widget_dict.get("plan") or output_dict.get("plan") or ""),
        "function": function_name,
        "tsmFunction": function_name,
        "naviMcp": "",
        "streamType": str(widget_dict.get("streamType") or ""),
        "llmOutputType": str(widget_dict.get("llmOutputType") or ""),
        "needsUserInput": needs_user_input,
        "candidateCount": candidate_count,
        "task": str(turn.get("task") or request_dict.get("task") or ""),
        "skill": str(turn.get("skill") or ""),
        "isAlreadyResponse": output_dict.get("isAlreadyResponse"),
        "raw": turn,
    }


def _extract_history_window(
    payloads: Iterable[Any], current_record_id: str | None = None, before: int = 2, after: int = 2
) -> list[dict[str, Any]]:
    candidates = _history_items(payloads)
    if not candidates or (before <= 0 and after <= 0):
        return []

    current_timestamp = _record_timestamp(current_record_id)
    unique: list[tuple[int, dict[str, Any]]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(candidates):
        record_id = str(item.get("recordId") or "")
        if record_id and record_id == str(current_record_id or ""):
            continue
        if record_id and record_id in seen_ids:
            continue
        if record_id:
            seen_ids.add(record_id)
        unique.append((index, item))

    if current_timestamp is None:
        # Without a timestamp in the current ID there is no defensible way to
        # distinguish a future turn from an older one.
        unique.sort(
            key=lambda pair: (_history_timestamp(pair[1].get("timestamp")) or -1, pair[0]),
            reverse=True,
        )
        return [_summarize_history_turn(item) for _, item in unique[:before]]

    older = [
        pair for pair in unique
        if (_history_timestamp(pair[1].get("timestamp")) is not None
            and _history_timestamp(pair[1].get("timestamp")) < current_timestamp)
    ]
    newer = [
        pair for pair in unique
        if (_history_timestamp(pair[1].get("timestamp")) is not None
            and _history_timestamp(pair[1].get("timestamp")) > current_timestamp)
    ]
    older.sort(
        key=lambda pair: (_history_timestamp(pair[1].get("timestamp")) or -1, pair[0]),
        reverse=True,
    )
    newer.sort(key=lambda pair: (_history_timestamp(pair[1].get("timestamp")) or -1, pair[0]))
    selected_older = list(reversed(older[:before]))
    selected_newer = newer[:after]
    return [_summarize_history_turn(item) for _, item in [*selected_older, *selected_newer]]


def _extract_previous_turn(payloads: Iterable[Any], current_record_id: str | None = None) -> dict[str, Any] | None:
    turns = _extract_history_window(payloads, current_record_id=current_record_id, before=1, after=0)
    return turns[0] if turns else None


def _extract_input(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in entries:
        text = _message(entry)
        for marker in (
            "收到原始input:", "上游请求内容:", "LLM.UP.REQUEST:",
            "鏀跺埌鍘熷input:", "涓婃父璇锋眰鍐呭:",
        ):
            payload = _json_after(text, marker)
            if not isinstance(payload, dict):
                continue
            request_payload = payload.get("request") if isinstance(payload.get("request"), dict) else {}
            inputs = request_payload.get("inputs") or payload.get("inputs")
            if not isinstance(inputs, list) or not inputs or not isinstance(inputs[0], dict):
                continue
            first = inputs[0]
            request = ((first.get("functions") or {}).get("request") or {})
            return {
                "text": str(first.get("input") or first.get("rec") or ""),
                "rec": str(first.get("rec") or ""),
                "source": str(first.get("source") or ""),
                "function": str(request.get("name") or ""),
                "functionDetail": request if isinstance(request, dict) else {},
                "upstreamResult": str(first.get("res") or ""),
            }
    for entry in entries:
        match = USER_INPUT_RE.search(_message(entry))
        if match:
            return {"text": match.group(1).strip(), "rec": "", "source": "", "function": "", "functionDetail": {}, "upstreamResult": ""}
    return None


def _clean_message(text: str, limit: int = 900) -> str:
    cleaned = HEADER_RE.sub("", text, count=1)
    cleaned = RECORD_TAIL_RE.sub("", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def _extract_final_output(entries: list[dict[str, Any]]) -> str:
    results: list[str] = []
    for entry in entries:
        text = _message(entry)
        marker = "NLG最终输出"
        if marker in text:
            value = text.split(marker, 1)[1].lstrip("(模拟-导航):： ")
            value = RECORD_TAIL_RE.sub("", value).strip()
            if value:
                results.append(value)
        if "LLM.UP.REQUEST:" in text or "收到完整请求:" in text or "上游请求内容:" in text:
            continue
        if not any(marker in text for marker in ("发送响应", "LLM.UP.RESPONSE", "NLG最终输出", '"streamType":"done"', '"streamType": "done"')):
            continue
        start = text.find("{")
        if start < 0:
            continue
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        for _, value in _walk(payload):
            if not isinstance(value, dict) or str(value.get("streamType") or "").lower() != "done":
                continue
            display_text = value.get("displayText")
            if isinstance(display_text, str) and display_text.strip():
                results.append(display_text.strip())
    return results[-1] if results else ""


def _extract_events(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        text = _message(entry)
        if "REQUEST_PAYLOAD" in text or "LLM.UP.REQUEST:" in text or "收到原始input:" in text:
            continue
        for category, title, pattern in EVENT_RULES:
            if not pattern.search(text):
                continue
            message = _clean_message(text)
            key = (title, message)
            if key in seen:
                break
            seen.add(key)
            events.append(
                {
                    "timestamp": str(entry.get("_time") or entry.get("time") or ""),
                    "service": str(entry.get("pod_project") or entry.get("app_name") or ""),
                    "category": category,
                    "title": title,
                    "message": message,
                }
            )
            break
    return events[:160]


def _extract_last_mcp(entries: list[dict[str, Any]]) -> str:
    tool_entry = _last_matching_entry(
        entries,
        r"鎵ц宸ュ叿璋冪敤:",
        r"鍙戦€?.* 鍒?MCP",
        r"MCP璋冪敤:",
        r"navigateToAddress 鍙傛暟",
        r"addWaypoint 鍙傛暟",
        r"MapApiTrigger.*鍙傛暟",
    )
    return _clean_message(_message(tool_entry), 260) if tool_entry else ""


def analyze_navi_flow(
    entries: Iterable[dict[str, Any]],
    current_record_id: str | None = None,
    plan_type: str | None = None,
) -> dict[str, Any]:
    entry_list = sorted(list(entries), key=lambda item: str(item.get("_time") or item.get("time") or ""))
    payloads = []
    for entry in entry_list:
        text = _message(entry)
        for marker in (
            "LLM.UP.REQUEST:", "收到完整请求:", "上游请求内容:",
            "鏀跺埌瀹屾暣璇锋眰:", "涓婃父璇锋眰鍐呭:",
        ):
            payload = _json_after(text, marker)
            if payload is not None:
                payloads.append(payload)
    input_info = _extract_input(entry_list)
    recent_turns = _extract_history_window(payloads, current_record_id=current_record_id, before=2, after=2)
    current_timestamp = _record_timestamp(current_record_id)
    history_before = [
        turn for turn in recent_turns
        if current_timestamp is None
        or _history_timestamp(turn.get("timestamp")) is None
        or _history_timestamp(turn.get("timestamp")) < current_timestamp
    ]
    history_after = [
        turn for turn in recent_turns
        if current_timestamp is not None
        and _history_timestamp(turn.get("timestamp")) is not None
        and _history_timestamp(turn.get("timestamp")) > current_timestamp
    ]
    route_summary = _extract_route_summary(entry_list, input_info, plan_type)
    last_mcp = _extract_last_mcp(entry_list)
    if not last_mcp:
        execution_nodes = [node for node in route_summary.get("nodes", []) if node.get("key") == "execution"]
        if execution_nodes:
            last_mcp = str(execution_nodes[-1].get("detail") or "")
    return {
        "previousTurn": (
            history_before[-1] if current_timestamp is not None and history_before
            else history_before[0] if history_before else None
        ),
        "recentTurns": recent_turns,
        "historyBefore": history_before,
        "historyAfter": history_after,
        "naviInfo": _find_naviinfo(payloads),
        "input": input_info,
        "tsmFunction": (input_info or {}).get("function") or "",
        "lastMcp": last_mcp,
        "routeSummary": route_summary,
        "events": _extract_events(entry_list),
        "finalOutput": _extract_final_output(entry_list),
        "rawLogCount": len(entry_list),
    }
