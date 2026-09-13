from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from flow_analysis import analyze_navi_flow
from plan_analysis import analyze


APP_DIR = Path(__file__).resolve().parent
SKILL_CLIENT = Path.home() / ".codex" / "skills" / "stdout-log-analysis" / "scripts" / "stdout_client.py"
D_AGENT_PROJECTS = ("d-agent", "d-agent-prod", "d-agent-test")
SERVICE_SCOPES = {
    "auto": (*D_AGENT_PROJECTS, "naviag-dev", "naviag", "llm-master-agent"),
    "dev": (*D_AGENT_PROJECTS, "naviag-dev", "llm-master-agent"),
    "prod": (*D_AGENT_PROJECTS, "naviag", "llm-master-agent"),
}
NAVI_LOG_SERVICES = ("naviag", "naviag-dev")
CHINA_TZ = timezone(timedelta(hours=8))


def scope_query(keyword: str, scope: str) -> str:
    """Build a regex project selector covering all D-agent deployments."""
    projects = SERVICE_SCOPES[scope]
    selector = "|".join(project.replace(".", r"\.") for project in projects)
    return (
        f'{{pod_project=~"{selector}"}} AND "{keyword}"'
        " | fields _time, pod_name, pod_project, k8scluster, _msg"
    )


def load_stdout_client():
    if not SKILL_CLIENT.exists():
        raise RuntimeError(f"找不到 stdout-log-analysis 客户端：{SKILL_CLIENT}")
    spec = importlib.util.spec_from_file_location("codex_stdout_client", SKILL_CLIENT)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 stdout-log-analysis 客户端")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def day_window(value: str) -> tuple[str, str]:
    try:
        selected = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期格式必须为 YYYY-MM-DD") from exc
    if selected > datetime.now(CHINA_TZ).date():
        raise ValueError("不能查询未来日期")
    start = datetime.combine(selected, time.min, tzinfo=CHINA_TZ)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def normalize_record_id(value: str) -> tuple[str, str]:
    record_id = value.strip()
    if not record_id or len(record_id) > 512:
        raise ValueError("请输入有效的日志 ID")
    if any(char in record_id for char in ('"', "'", "\n", "\r")):
        raise ValueError("日志 ID 含有不支持的字符")
    root_id = record_id.split(":", 1)[0]
    if len(root_id) < 8:
        raise ValueError("日志 ID 过短")
    return record_id, root_id


def query_logs(payload: dict[str, Any]) -> dict[str, Any]:
    record_id, root_id = normalize_record_id(str(payload.get("recordId") or ""))
    start, end = day_window(str(payload.get("date") or ""))
    scope = str(payload.get("scope") or "auto")
    services = SERVICE_SCOPES.get(scope)
    if services is None:
        raise ValueError("未知的查询环境")

    client = load_stdout_client()
    try:
        env = client.load_env()
    except PermissionError as exc:
        raise RuntimeError(
            "无法读取本机 stdout-log 凭据，请在有权限的本地 PowerShell 中启动此工具"
        ) from exc
    query = scope_query(root_id, scope)
    entries, diagnostic = client.post_logsql(query, start, end, 4000, env, "stdout")
    raw_entries = [entry.raw for entry in entries]
    result = analyze(raw_entries)
    primary_flow = analyze_navi_flow(
        raw_entries, current_record_id=record_id, plan_type=result.get("plan")
    )
    navi_logs: list[dict[str, str]] = []
    navi_flow: dict[str, Any] | None = None
    if result.get("plan") == "simple_navi":
        navi_query = client.logsql_query(root_id, NAVI_LOG_SERVICES, fields=True, log_type="stdout")
        navi_entries, navi_diagnostic = client.post_logsql(
            navi_query, start, end, 4000, env, "stdout"
        )
        for entry in navi_entries[:400]:
            navi_logs.append(
                {
                    "timestamp": entry.timestamp or "",
                    "service": entry.pod_project or "",
                    "message": entry.text,
                }
            )
        navi_flow = analyze_navi_flow(
            (entry.raw for entry in navi_entries),
            current_record_id=record_id,
        )
    else:
        navi_query = ""
        navi_diagnostic = ""
    result.update(
        {
            "recordId": record_id,
            "searchedId": root_id,
            "date": payload["date"],
            "start": start,
            "end": end,
            "scope": scope,
            "indexedServices": list(services),
            "query": query,
            "diagnostic": diagnostic,
            "naviLogs": navi_logs,
            "naviFlow": navi_flow,
            "naviRouteSummary": (navi_flow or {}).get("routeSummary"),
            "previousTurn": primary_flow.get("previousTurn"),
            "recentTurns": _build_recent_turns(
                record_id,
                result.get("plan"),
                (
                    navi_flow
                    if result.get("plan") == "simple_navi" and navi_flow is not None
                    else {**primary_flow, "lastMcp": ""}
                ),
                primary_flow.get("historyBefore") or [],
                primary_flow.get("historyAfter") or [],
            ),
            "naviQuery": navi_query,
            "naviDiagnostic": navi_diagnostic,
        }
    )
    return result


def _build_recent_turns(
    record_id: str,
    plan: Any,
    current_flow: dict[str, Any],
    history_before: list[dict[str, Any]],
    history_after: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a centered five-turn window: two before, current, two after."""
    older = history_before[-2:]
    newer = history_after[:2]
    current = {
        "recordId": record_id,
        "timestamp": None,
        "input": str((current_flow.get("input") or {}).get("text") or ""),
        "plan": str(plan or ""),
        "tsmFunction": str(current_flow.get("tsmFunction") or ""),
        "function": str(current_flow.get("tsmFunction") or ""),
        "naviMcp": str(current_flow.get("lastMcp") or ""),
        "isCurrent": True,
    }
    for turn in (*older, *newer):
        turn["isCurrent"] = False
    return [*older, current, *newer]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True, "skillClient": str(SKILL_CLIENT)})
            return
        if self.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/search":
            self._json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 16_384:
                raise ValueError("请求体无效")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            self._json(HTTPStatus.OK, query_logs(payload))
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="本地 Plan 分类日志查询器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Plan 日志查询器已启动：http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
