from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any, Iterable


PLAN_KEYS = {"plan", "planType", "llmPlan", "routePlan"}
PLAN_FIELDS = PLAN_KEYS | {"plannerResult", "subAgent"}
MULTI_KEYS = {"isMultiIntent", "multi_intent"}
PLAN_VALUE_RE = re.compile(r"\bplan=([A-Za-z0-9_-]*)\s+multi_intent=(true|false)", re.I)
PLANNER_RESULT_RE = re.compile(
    r'KEYLOG\.PLANNER\.RESULT:.*?\bresult="<DAG>(.*?)</DAG>"',
    re.I | re.S,
)
SUB_AGENT_RE = re.compile(r"\bsub_agent=([A-Za-z0-9_-]+)", re.I)


@dataclass(frozen=True)
class Evidence:
    timestamp: str
    service: str
    source: str
    field: str
    value: str
    priority: int
    excerpt: str
    path: str = ""


def _message(entry: dict[str, Any]) -> str:
    return str(entry.get("_msg") or entry.get("msg") or entry.get("message") or "")


def _excerpt(text: str, needle: str, limit: int = 560) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    index = compact.lower().find(needle.lower())
    if index < 0:
        return compact[: limit - 3] + "..."
    start = max(0, index - limit // 3)
    end = min(len(compact), start + limit)
    start = max(0, end - limit)
    prefix = "..." if start else ""
    suffix = "..." if end < len(compact) else ""
    return prefix + compact[start:end] + suffix


def _json_payload(text: str) -> Any | None:
    markers = ("LLM.UP.REQUEST:", "收到完整请求:", "received request:")
    start = -1
    for marker in markers:
        marker_index = text.lower().find(marker.lower())
        if marker_index >= 0:
            start = text.find("{", marker_index + len(marker))
            break
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
        return value
    except json.JSONDecodeError:
        return None


def _walk_signals(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[str, str, str, int]]:
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = path + (str(key),)
            path_text = ".".join(child_path)
            in_history = any(part == "dialogHistory" for part in child_path)
            if key in PLAN_KEYS and isinstance(item, str) and item.strip():
                priority = 25 if in_history else 85
                yield key, item.strip(), path_text, priority
            elif key in MULTI_KEYS and isinstance(item, (bool, int, float, str)):
                normalized = str(item).lower() if not isinstance(item, bool) else str(item).lower()
                priority = 25 if in_history else 90
                yield key, normalized, path_text, priority
            yield from _walk_signals(item, child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_signals(item, path + (str(index),))


def extract_evidence(entries: Iterable[dict[str, Any]]) -> list[Evidence]:
    evidence: list[Evidence] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for entry in entries:
        text = _message(entry)
        timestamp = str(entry.get("_time") or entry.get("time") or "")
        service = str(entry.get("pod_project") or entry.get("app_name") or "")

        planner_match = PLANNER_RESULT_RE.search(text)
        if planner_match:
            dag_content = planner_match.group(1).strip()
            task_names = [
                task.strip().split(":", 1)[0].strip()
                for task in dag_content.split("||")
                if task.strip() and task.strip() != "REACT"
            ]
            for task_index, task_name in enumerate(task_names):
                key = (timestamp, service, "Planner result", "plannerResult", task_name)
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    Evidence(
                        timestamp=timestamp,
                        service=service,
                        source="Planner result",
                        field="plannerResult",
                        value=task_name,
                        priority=120 - task_index,
                        excerpt=_excerpt(text, "KEYLOG.PLANNER.RESULT"),
                    )
                )
            multi_value = "true" if len(task_names) > 1 else "false"
            key = (timestamp, service, "Planner result", "isMultiIntent", multi_value)
            if key not in seen:
                seen.add(key)
                evidence.append(
                    Evidence(
                        timestamp=timestamp,
                        service=service,
                        source="Planner result",
                        field="isMultiIntent",
                        value=multi_value,
                        priority=120,
                        excerpt=_excerpt(text, "KEYLOG.PLANNER.RESULT"),
                    )
                )

        if "LLM.DOWN.REQUEST:" in text or "ARBITRATION.DOWNSTREAM_FUNCTIONS:" in text:
            sub_agent_match = SUB_AGENT_RE.search(text)
            if sub_agent_match:
                sub_agent = sub_agent_match.group(1)
                key = (timestamp, service, "Dispatch", "subAgent", sub_agent)
                if key not in seen:
                    seen.add(key)
                    evidence.append(
                        Evidence(
                            timestamp=timestamp,
                            service=service,
                            source="Dispatch",
                            field="subAgent",
                            value=sub_agent,
                            priority=95,
                            excerpt=_excerpt(text, "sub_agent="),
                        )
                    )

        decision_match = PLAN_VALUE_RE.search(text)
        if decision_match:
            plan_value, multi_value = decision_match.groups()
            for field, value in (("plan", plan_value or "(empty)"), ("isMultiIntent", multi_value.lower())):
                key = (timestamp, service, "运行时判定", field, value)
                if key not in seen:
                    seen.add(key)
                    evidence.append(
                        Evidence(
                            timestamp=timestamp,
                            service=service,
                            source="运行时判定",
                            field=field,
                            value=value,
                            priority=100,
                            excerpt=_excerpt(text, "plan="),
                        )
                    )

        payload = _json_payload(text)
        if payload is None:
            continue
        for field, value, path, priority in _walk_signals(payload):
            source = "历史对话" if "dialogHistory" in path.split(".") else "请求报文"
            key = (timestamp, service, source, field, value)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(
                Evidence(
                    timestamp=timestamp,
                    service=service,
                    source=source,
                    field=field,
                    value=value,
                    priority=priority,
                    excerpt=_excerpt(text, f'"{field}"'),
                    path=path,
                )
            )

    evidence.sort(key=lambda item: (-item.priority, item.timestamp, item.field, item.value))
    return evidence


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_multi_plan(plan: str) -> bool:
    normalized = plan.strip().lower().replace("-", "_")
    return normalized == "multi" or normalized.startswith("multi_") or "_multi_" in normalized


def analyze(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    entry_list = list(entries)
    evidence = extract_evidence(entry_list)
    strong = [item for item in evidence if item.priority >= 80]
    plan_item = next((item for item in strong if item.field in PLAN_FIELDS and item.value != "(empty)"), None)
    multi_item = next((item for item in strong if item.field in MULTI_KEYS), None)
    plan = plan_item.value if plan_item else ""
    is_multi_intent = _truthy(multi_item.value) if multi_item else None

    if not plan and is_multi_intent is None:
        verdict = "unknown"
        reason = "命中日志中没有发现当前请求的强分类证据"
    elif _is_multi_plan(plan):
        verdict = "multi"
        reason = f"plan={plan} 属于 multi 类型"
    elif is_multi_intent is True:
        verdict = "multi"
        reason = "isMultiIntent=true"
    else:
        verdict = "single"
        reason = f"plan={plan}" if plan else "isMultiIntent=false"

    services = sorted({str(entry.get("pod_project") or entry.get("app_name") or "") for entry in entry_list if entry})
    return {
        "verdict": verdict,
        "reason": reason,
        "plan": plan or None,
        "isMultiIntent": is_multi_intent,
        "logCount": len(entry_list),
        "services": [service for service in services if service],
        "evidence": [asdict(item) for item in evidence[:80]],
        "strongEvidenceCount": len(strong),
    }
