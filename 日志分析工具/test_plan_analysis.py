from __future__ import annotations

import unittest
from unittest.mock import patch

from plan_analysis import analyze
from server import day_window, normalize_record_id, query_logs, scope_query


class PlanAnalysisTest(unittest.TestCase):
    def test_planner_result_is_authoritative_for_upstream_only_logs(self) -> None:
        result = analyze(
            [
                {
                    "_time": "2026-09-03T02:03:52Z",
                    "pod_project": "d-agent",
                    "_msg": (
                        'KEYLOG.PLANNER.RESULT: record_id=abc input="route request" '
                        'result="<DAG>multi_poi_navi</DAG>" timing={"tasks_count":1}'
                    ),
                },
                {
                    "_time": "2026-09-03T02:03:53Z",
                    "pod_project": "d-agent",
                    "_msg": "LLM.DOWN.REQUEST: sub_agent=multi_poi_navi, websocket_url=wss://example.invalid",
                },
            ]
        )
        self.assertEqual("multi", result["verdict"])
        self.assertEqual("multi_poi_navi", result["plan"])
        self.assertFalse(result["isMultiIntent"])
        self.assertEqual("Planner result", result["evidence"][0]["source"])

    def test_multi_task_dag_sets_multi_intent(self) -> None:
        result = analyze(
            [
                {
                    "_time": "2026-09-03T02:03:52Z",
                    "pod_project": "d-agent",
                    "_msg": (
                        'KEYLOG.PLANNER.RESULT: result="<DAG>multi_poi_navi:route '
                        '|| car_control:air conditioner</DAG>"'
                    ),
                }
            ]
        )
        self.assertEqual("multi_poi_navi", result["plan"])
        self.assertTrue(result["isMultiIntent"])

    def test_runtime_decision_wins_over_history(self) -> None:
        entries = [
            {
                "_time": "2026-09-03T01:00:00Z",
                "pod_project": "naviag-dev",
                "_msg": 'LLM.UP.REQUEST: {"dialogHistory":[{"output":{"widget":{"plan":"simple_navi"}}}]}',
            },
            {
                "_time": "2026-09-03T01:00:01Z",
                "pod_project": "naviag-dev",
                "_msg": "early streaming 垫句判定: should_start=False plan=multi_poi_navi multi_intent=False extra={}",
            },
        ]
        result = analyze(entries)
        self.assertEqual("multi", result["verdict"])
        self.assertEqual("multi_poi_navi", result["plan"])
        self.assertFalse(result["isMultiIntent"])

    def test_multi_intent_is_distinct_multi_signal(self) -> None:
        result = analyze(
            [
                {
                    "_time": "2026-09-03T01:00:01Z",
                    "pod_project": "naviag",
                    "_msg": "early streaming 垫句判定: plan=simple_navi multi_intent=True",
                }
            ]
        )
        self.assertEqual("multi", result["verdict"])
        self.assertEqual("simple_navi", result["plan"])
        self.assertTrue(result["isMultiIntent"])

    def test_history_only_does_not_claim_current_plan(self) -> None:
        result = analyze(
            [
                {
                    "_time": "2026-09-03T01:00:00Z",
                    "pod_project": "naviag-dev",
                    "_msg": 'LLM.UP.REQUEST: {"dialogHistory":[{"output":{"widget":{"plan":"multi_poi_navi"}}}]}',
                }
            ]
        )
        self.assertEqual("unknown", result["verdict"])
        self.assertIsNone(result["plan"])
        self.assertEqual("历史对话", result["evidence"][0]["source"])

    def test_current_request_json_is_strong_evidence(self) -> None:
        result = analyze(
            [
                {
                    "_time": "2026-09-03T01:00:00Z",
                    "pod_project": "naviag",
                    "_msg": 'LLM.UP.REQUEST: {"context":{"attributes":{"isMultiIntent":false}},"output":{"plan":"fuzzy"}}',
                }
            ]
        )
        self.assertEqual("single", result["verdict"])
        self.assertEqual("fuzzy", result["plan"])
        self.assertFalse(result["isMultiIntent"])


class ValidationTest(unittest.TestCase):
    def test_scope_query_uses_d_agent_regex_projects(self) -> None:
        query = scope_query("record-123", "auto")
        self.assertIn(
            'pod_project=~"d-agent|d-agent-prod|d-agent-test|naviag-dev|naviag|llm-master-agent"',
            query,
        )
        self.assertIn('AND "record-123"', query)

    def test_date_creates_one_china_timezone_day(self) -> None:
        start, end = day_window("2026-09-03")
        self.assertEqual("2026-09-03T00:00:00+08:00", start)
        self.assertEqual("2026-09-04T00:00:00+08:00", end)

    def test_composite_id_uses_first_segment_for_route_search(self) -> None:
        full, root = normalize_record_id("a" * 32 + ":" + "b" * 32)
        self.assertIn(":", full)
        self.assertEqual("a" * 32, root)

    def test_query_injection_characters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_record_id('abc12345" AND "error')

    @patch("server.load_stdout_client")
    def test_query_logs_indexes_all_routing_services(self, load_client) -> None:
        class Entry:
            raw = {
                "_time": "2026-09-03T01:00:01Z",
                "pod_project": "naviag-dev",
                "_msg": "early streaming decision: plan=multi_poi_navi multi_intent=False",
            }

        class Client:
            @staticmethod
            def load_env():
                return {}

            @staticmethod
            def logsql_query(keyword, services, fields, log_type):
                self.assertEqual("a" * 32, keyword)
                self.assertEqual(
                    ("naviag-dev", "naviag", "d-agent", "llm-master-agent"),
                    services,
                )
                return '{pod_project in ("naviag-dev", "naviag", "d-agent", "llm-master-agent")} AND "root"'

            @staticmethod
            def post_logsql(query, start, end, limit, env, log_type):
                self.assertEqual(4000, limit)
                self.assertEqual("2026-09-03T00:00:00+08:00", start)
                self.assertEqual("2026-09-04T00:00:00+08:00", end)
                return [Entry()], "fake diagnostic"

        load_client.return_value = Client()
        result = query_logs(
            {
                "recordId": "a" * 32 + ":" + "b" * 32,
                "date": "2026-09-03",
                "scope": "auto",
            }
        )
        self.assertEqual("multi", result["verdict"])
        self.assertEqual("multi_poi_navi", result["plan"])
        self.assertEqual("a" * 32, result["searchedId"])
        self.assertEqual([], result["naviLogs"])
        self.assertIsNone(result["naviRouteSummary"])

    @patch("server.load_stdout_client")
    def test_simple_navi_queries_and_returns_navi_logs(self, load_client) -> None:
        class Entry:
            def __init__(self, raw):
                self.raw = raw
                self.timestamp = raw.get("_time")
                self.pod_project = raw.get("pod_project")
                self.text = raw.get("_msg")

        class Client:
            calls = []

            @staticmethod
            def load_env():
                return {}

            @classmethod
            def logsql_query(cls, keyword, services, fields, log_type):
                cls.calls.append(services)
                return str(services)

            @staticmethod
            def post_logsql(query, start, end, limit, env, log_type):
                if "naviag-dev" in query and "d-agent" not in query:
                    return [
                        Entry(
                            {
                                "_time": "2026-09-03T02:03:53Z",
                                "pod_project": "naviag-dev",
                                "_msg": "收到 simple_navi 请求",
                            }
                        )
                    ], "navi diagnostic"
                return [
                    Entry(
                        {
                            "_time": "2026-09-03T02:03:52Z",
                            "pod_project": "d-agent",
                            "_msg": 'KEYLOG.PLANNER.RESULT: result="<DAG>simple_navi</DAG>"',
                        }
                    )
                ], "planner diagnostic"

        load_client.return_value = Client()
        result = query_logs(
            {
                "recordId": "a" * 32,
                "date": "2026-09-03",
                "scope": "auto",
            }
        )
        self.assertEqual("simple_navi", result["plan"])
        self.assertEqual("single", result["verdict"])
        self.assertEqual("naviag-dev", result["naviLogs"][0]["service"])
        self.assertEqual("收到 simple_navi 请求", result["naviLogs"][0]["message"])
        self.assertIn(("naviag", "naviag-dev"), Client.calls)
        self.assertIsNotNone(result["naviRouteSummary"])
        self.assertNotIn("Planner 分类", result["naviRouteSummary"]["headline"])


if __name__ == "__main__":
    unittest.main()
