#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SECRET = Path.home() / ".codex" / "secrets" / "stdout-log.env"
DEFAULT_OUTPUT = Path("/Volumes/D/logs")
DEFAULT_APIS = {
    "stdout": "https://vlogs-api.aispeech.com/stdout/select/logsql/query",
    "syslog": "https://vlogs-api.aispeech.com/syslog/select/logsql/query",
}
SPECIAL_TRACE_SERVICES = {"llm-master-agent", "lyra-auto-agent"}
DEFAULT_STDOUT_PROJECTS = ("d-agent", "llm-master-agent", "naviag")
ServiceSelector = str | tuple[str, ...] | None


@dataclasses.dataclass
class LogEntry:
    raw: dict[str, Any]
    text: str
    timestamp: str | None
    pod_name: str | None
    pod_project: str | None
    k8scluster: str | None
    elapsed_ms: int | None
    logger: str | None
    level: str | None


def load_env(path: Path = DEFAULT_SECRET) -> dict[str, str]:
    env = dict(os.environ)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip())
    return env


def auth_header(env: dict[str, str]) -> str:
    user = env.get("STDOUT_LOG_USERNAME")
    password = env.get("STDOUT_LOG_PASSWORD")
    if not user or not password:
        raise RuntimeError(f"missing STDOUT_LOG_USERNAME/STDOUT_LOG_PASSWORD in {DEFAULT_SECRET}")
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {encoded}"


def api_for_log_type(log_type: str, env: dict[str, str]) -> str:
    if log_type == "stdout":
        return env.get("STDOUT_LOG_BASE_URL", DEFAULT_APIS["stdout"])
    if log_type == "syslog":
        return env.get("SYSLOG_LOG_BASE_URL", DEFAULT_APIS["syslog"])
    raise ValueError(f"unsupported log type: {log_type}")


def service_index_field(log_type: str) -> str:
    return "app_name" if log_type == "syslog" else "pod_project"


def default_fields(log_type: str) -> str:
    if log_type == "syslog":
        return "_time, app_name, k8scluster, _msg"
    return "_time, pod_name, pod_project, k8scluster, _msg"


def service_selector_expr(log_type: str, service: ServiceSelector) -> str | None:
    if not service:
        return None
    field = service_index_field(log_type)
    if isinstance(service, tuple):
        values = [value for value in service if value]
        if not values:
            return None
        if len(values) == 1:
            return f'{field}="{values[0]}"'
        joined = ", ".join(f'"{value}"' for value in values)
        return f"{field} in ({joined})"
    return f'{field}="{service}"'


def logsql_query(
    keyword: str,
    service: ServiceSelector = None,
    cluster: str | None = None,
    fields: bool = True,
    log_type: str = "stdout",
) -> str:
    selectors = []
    if cluster:
        selectors.append(f'k8scluster="{cluster}"')
    service_expr = service_selector_expr(log_type, service)
    if service_expr:
        selectors.append(service_expr)
    selector = "{" + ", ".join(selectors) + "}" if selectors else ""
    query = f'{selector} AND "{keyword}"' if selector else f'"{keyword}"'
    if fields:
        query += f" | fields {default_fields(log_type)}"
    return query


def post_logsql(query: str, start: str, end: str, limit: int, env: dict[str, str], log_type: str = "stdout") -> tuple[list[LogEntry], str]:
    api = api_for_log_type(log_type, env)
    data = urllib.parse.urlencode({"query": query, "start": start, "end": end, "limit": str(limit)}).encode()
    req = urllib.request.Request(api, data=data, method="POST")
    req.add_header("Authorization", auth_header(env))
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/stream+json,application/json,text/plain,*/*")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            return parse_stream_json(body), f"POST {api} -> {resp.status} {resp.headers.get('content-type')} query={query!r}"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return [], f"POST {api} -> {exc.code} {body[:500]!r} query={query!r}"


def parse_stream_json(body: str) -> list[LogEntry]:
    entries: list[LogEntry] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            raw = {"_msg": line}
        msg = str(raw.get("_msg") or raw.get("msg") or raw.get("message") or raw)
        parsed = parse_msg_header(msg)
        entries.append(
            LogEntry(
                raw=raw,
                text=msg,
                timestamp=str(raw.get("_time") or raw.get("time") or raw.get("timestamp") or "") or None,
                pod_name=as_str(raw.get("pod_name")),
                pod_project=as_str(raw.get("pod_project") or raw.get("app_name")),
                k8scluster=as_str(raw.get("k8scluster")),
                elapsed_ms=parsed.get("elapsed_ms"),
                logger=parsed.get("logger"),
                level=parsed.get("level"),
            )
        )
    entries.sort(key=lambda e: (e.timestamp or "", e.elapsed_ms if e.elapsed_ms is not None else -1))
    return entries


def as_str(value: Any) -> str | None:
    return None if value is None else str(value)


HEADER_RE = re.compile(
    r"^(?P<local_time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)-"
    r"(?P<level>[A-Z]+)-\[(?P<logger>[^\]]+)\]-\[(?P<trace>[^\]]*)\]-"
    r"\[(?P<path>[^\]]*)\]-\[(?P<elapsed>\d+)ms\] (?P<body>.*)$",
    re.S,
)


def parse_msg_header(msg: str) -> dict[str, Any]:
    match = HEADER_RE.match(msg)
    if not match:
        return {}
    return {
        "level": match.group("level"),
        "logger": match.group("logger"),
        "elapsed_ms": int(match.group("elapsed")),
        "body": match.group("body"),
    }


TRACE_PATTERNS = [
    re.compile(r'["\'](?:traceId|trace_id|trace-id|trace_id)["\']\s*[:=]\s*["\']?([A-Za-z0-9_.:-]{8,128})'),
    re.compile(r"\b(?:traceId|trace_id|trace-id)\s*[:=]\s*([A-Za-z0-9_.:-]{8,128})"),
    re.compile(r"-\[([0-9a-fA-F]{16,64})\]-\[/"),
]


def extract_trace_id(entries: Iterable[LogEntry]) -> str | None:
    for entry in entries:
        for pattern in TRACE_PATTERNS:
            match = pattern.search(entry.text)
            if match:
                return match.group(1).rstrip(",:;)")
    return None


def search_with_fallback(
    keyword: str,
    service: str | None,
    cluster: str | None,
    start: str,
    end: str,
    limit: int,
    env: dict[str, str],
    log_type: str = "stdout",
    allow_global_fallback: bool = False,
) -> tuple[list[LogEntry], list[str]]:
    diagnostics: list[str] = []
    attempts: list[tuple[ServiceSelector, str | None]] = []
    if service and cluster:
        attempts.append((service, cluster))
    if service:
        attempts.append((service, None))
    elif log_type == "stdout":
        if cluster:
            attempts.append((DEFAULT_STDOUT_PROJECTS, cluster))
        attempts.append((DEFAULT_STDOUT_PROJECTS, None))
    elif cluster:
        attempts.append((None, cluster))
    if allow_global_fallback:
        attempts.append((None, None))

    seen = set()
    for svc, clu in attempts:
        key = (svc, clu)
        if key in seen:
            continue
        seen.add(key)
        query = logsql_query(keyword, svc, clu, log_type=log_type)
        entries, diag = post_logsql(query, start, end, limit, env, log_type)
        diagnostics.append(diag + f" hits={len(entries)}")
        if entries:
            return entries, diagnostics
    return [], diagnostics


def save_jsonl(entries: list[LogEntry], name: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Windows filenames cannot contain the colon separators used by record IDs.
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)[:180]
    path = output_dir / f"{safe}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry.raw, ensure_ascii=False) + "\n")
    return path


def normalize_service(service: str | None) -> str:
    return (service or "").lower()


def is_llm_master_service(service: str | None) -> bool:
    return normalize_service(service) == "llm-master-agent"


def is_special_trace_service(service: str | None) -> bool:
    return normalize_service(service) in SPECIAL_TRACE_SERVICES


def has_special_trace_logs(entries: Iterable[LogEntry]) -> bool:
    return any(is_special_trace_service(entry.pod_project) for entry in entries)


def summarize(text: str, length: int = 180) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= length else text[: length - 3] + "..."


def latency_report(entries: list[LogEntry], target_record: str | None = None) -> dict[str, Any]:
    timed = [e for e in entries if e.elapsed_ms is not None]
    timed.sort(key=lambda e: e.elapsed_ms or 0)
    if not timed:
        return {"summary": "未找到可解析的 [xxxms] elapsed 字段。"}

    gaps = []
    for prev, curr in zip(timed, timed[1:]):
        gaps.append(
            {
                "gapMs": (curr.elapsed_ms or 0) - (prev.elapsed_ms or 0),
                "fromElapsedMs": prev.elapsed_ms,
                "toElapsedMs": curr.elapsed_ms,
                "from": summarize(prev.text, 120),
                "to": summarize(curr.text, 160),
            }
        )
    gaps.sort(key=lambda x: x["gapMs"], reverse=True)

    target_entries = [e for e in timed if target_record and target_record in e.text]
    target_start = min((e.elapsed_ms for e in target_entries if e.elapsed_ms is not None), default=None)
    target_end = max((e.elapsed_ms for e in target_entries if e.elapsed_ms is not None), default=None)

    markers = {}
    for label, words in {
        "websocket_start": ["WebSocket请求开始"],
        "llm_up_request": ["LLM.UP.REQUEST"],
        "task_sched": ["TASK_SCHED"],
        "agent_send": ["[WEBSOCKET_AGENT] send"],
        "agent_recv": ["[WEBSOCKET_AGENT] recv"],
        "send_client": ["send message to client"],
        "cleanup": ["已清理任务资源"],
        "websocket_end": ["WebSocket请求结束"],
        "error": ["ERROR", "Exception", "Traceback"],
    }.items():
        matched = [e for e in timed if any(word in e.text for word in words)]
        markers[label] = [
            {
                "elapsedMs": e.elapsed_ms,
                "time": e.timestamp,
                "logger": e.logger,
                "message": summarize(e.text, 220),
            }
            for e in matched[:10]
        ]

    return {
        "totalVisibleMs": (timed[-1].elapsed_ms or 0) - (timed[0].elapsed_ms or 0),
        "firstElapsedMs": timed[0].elapsed_ms,
        "lastElapsedMs": timed[-1].elapsed_ms,
        "targetRecordVisibleStartMs": target_start,
        "targetRecordVisibleEndMs": target_end,
        "targetRecordVisibleDurationMs": (target_end - target_start) if target_start is not None and target_end is not None else None,
        "largestGaps": gaps[:12],
        "markers": markers,
    }


def run_analyze(args: argparse.Namespace) -> int:
    env = load_env()
    start = args.start
    end = args.end
    service = args.service
    cluster = args.cluster
    log_type = args.log_type
    record_id = args.record_id
    diagnostics: list[str] = []

    record_entries, diags = search_with_fallback(record_id, service, cluster, start, end, args.limit, env, log_type, args.allow_global_fallback)
    diagnostics.extend(diags)

    trace_id = args.trace_id
    up_entries: list[LogEntry] = []
    if log_type == "stdout" and not trace_id:
        if is_llm_master_service(service) or args.llm_master:
            up_keyword = f"LLM.UP.REQUEST\" AND \"{record_id}"
            up_entries, diags = search_with_fallback(up_keyword, "llm-master-agent", cluster, start, end, args.limit, env, log_type, args.allow_global_fallback)
            diagnostics.extend(diags)
            trace_id = extract_trace_id(up_entries or record_entries)
        elif is_special_trace_service(service) or has_special_trace_logs(record_entries):
            trace_id = extract_trace_id(record_entries)

    is_special_trace_context = (
        args.llm_master
        or is_special_trace_service(service)
        or has_special_trace_logs(record_entries)
        or has_special_trace_logs(up_entries)
    )

    trace_entries: list[LogEntry] = []
    if trace_id:
        trace_service = None if log_type == "stdout" and is_special_trace_context else service
        trace_entries, diags = search_with_fallback(trace_id, trace_service, cluster, start, end, args.limit, env, log_type, args.allow_global_fallback)
        diagnostics.extend(diags)

    selected = trace_entries or up_entries or record_entries
    output_path = None
    trace_output_path = None
    output_dir = Path(args.output_dir)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.download or selected:
        output_path = save_jsonl(
            selected,
            f"{log_type}-{record_id}-{trace_id or 'no-trace'}-{stamp}",
            output_dir,
        )

    is_special_trace_context = is_special_trace_context or has_special_trace_logs(trace_entries)
    if trace_id and trace_entries and is_special_trace_context:
        trace_output_path = save_jsonl(
            trace_entries,
            f"{log_type}-trace-{trace_id}-{stamp}",
            output_dir,
        )

    result = {
        "recordId": record_id,
        "traceId": trace_id,
        "logType": log_type,
        "service": service,
        "defaultIndexedProjects": list(DEFAULT_STDOUT_PROJECTS) if log_type == "stdout" and not service else None,
        "allowGlobalFallback": args.allow_global_fallback,
        "cluster": cluster,
        "start": start,
        "end": end,
        "recordLogCount": len(record_entries),
        "llmUpRequestLogCount": len(up_entries),
        "traceLogCount": len(trace_entries),
        "selectedLogCount": len(selected),
        "logFile": str(output_path) if output_path else None,
        "traceLogFile": str(trace_output_path) if trace_output_path else None,
        "latency": latency_report(selected, record_id) if selected else {"summary": "未命中日志。"},
        "diagnostics": diagnostics,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if selected else 2


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Query AISpeech vLogs Stdout/Syslog logs via VictoriaLogs LogSQL API.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["analyze", "download"]:
        p = sub.add_parser(name)
        p.add_argument("--record-id", required=True)
        p.add_argument("--trace-id")
        p.add_argument("--log-type", choices=["stdout", "syslog"], default="stdout")
        p.add_argument("--service")
        p.add_argument("--allow-global-fallback", action="store_true")
        p.add_argument("--cluster")
        p.add_argument("--start", default="72h")
        p.add_argument("--end", default="now")
        p.add_argument("--limit", type=int, default=1000)
        p.add_argument("--question", default="")
        p.add_argument("--llm-master", action="store_true")
        p.add_argument("--download", action="store_true", default=name == "download")
        p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
        p.set_defaults(func=run_analyze)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
