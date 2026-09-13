from __future__ import annotations

import unittest

from flow_analysis import analyze_navi_flow


class NaviFlowAnalysisTest(unittest.TestCase):
    def test_route_summary_detects_upstream_fc_direct_execution(self) -> None:
        result = analyze_navi_flow(
            [
                {
                    "_time": "2026-09-03T01:00:00Z",
                    "_msg": (
                        '收到原始input: {"inputs":[{"input":"导航去苏州中心",'
                        '"functions":{"request":{"name":"navigateToAddress","params":[]}}}]}'
                    ),
                },
                {
                    "_time": "2026-09-03T01:00:01Z",
                    "_msg": "执行工具调用: navigateToAddress({'address':'苏州中心'})",
                },
            ]
        )
        route = result["routeSummary"]
        self.assertEqual("fc", route["upstreamType"])
        self.assertEqual("navigateToAddress", route["upstreamFunction"])
        self.assertEqual(["上游 FC", "执行动作"], [node["title"] for node in route["nodes"]])

    def test_route_summary_reads_upstream_request_and_res_deep(self) -> None:
        result = analyze_navi_flow(
            [
                {
                    "_msg": (
                        '上游请求内容: {"request":{"inputs":[{"input":"找附近餐厅",'
                        '"res":"deep","functions":null}]}}'
                    )
                }
            ]
        )
        self.assertEqual("找附近餐厅", result["input"]["text"])
        self.assertEqual("deep", result["input"]["upstreamResult"])
        self.assertEqual("deep", result["routeSummary"]["upstreamType"])
        self.assertEqual("上游 Deep", result["routeSummary"]["nodes"][0]["title"])

    def test_route_summary_detects_deep_semantic_loop_to_travel(self) -> None:
        result = analyze_navi_flow(
            [
                {
                    "_time": "2026-09-03T01:00:00Z",
                    "_msg": (
                        '收到原始input: {"inputs":[{"input":"帮我找个吃饭的地方",'
                        '"functions":{"request":{"name":"mapDeep","params":[]}}}]}'
                    ),
                },
                {
                    "_time": "2026-09-03T01:00:01Z",
                    "_msg": "semantic_loop mode=active relation=new_task action=search confidence=0.92 validation=ok",
                },
                {
                    "_time": "2026-09-03T01:00:02Z",
                    "_msg": "PID=123, mode=no_mcp, 路由到 TravelSkillAgent",
                },
            ]
        )
        titles = [node["title"] for node in result["routeSummary"]["nodes"]]
        self.assertEqual(["上游 Deep", "Semantic Loop 推理", "进入 Travel"], titles)

    def test_route_summary_detects_deep_rewrite_function_call(self) -> None:
        result = analyze_navi_flow(
            [
                {
                    "_msg": (
                        '收到原始input: {"inputs":[{"input":"改一下目的地",'
                        '"functions":{"request":{"name":"deep","params":[]}}}]}'
                    ),
                },
                {
                    "_msg": (
                        "KEYLOG.NAVI.DEEP_REWRITE.FUNCTION_CALL: skill=modify_destination "
                        "standard_query=修改目的地 function=modifyDestination params={'address':'公司'}"
                    )
                },
            ]
        )
        titles = [node["title"] for node in result["routeSummary"]["nodes"]]
        self.assertIn("Deep 改写并拆解 FC", titles)

    def test_route_summary_lists_cloud_search_sources(self) -> None:
        result = analyze_navi_flow(
            [
                {"_msg": "[火山候选流式搜索] 请求报文: redacted"},
                {"_msg": "[火山流式验证] triple_parallel_search 内部使用火山候选+高德反查"},
                {"_msg": "[WebSearch预发射] 已启动"},
                {"_msg": "AMap maps_text_search 返回 6 条"},
            ]
        )
        sources = {source["title"] for source in result["routeSummary"]["dataSources"]}
        self.assertEqual({"火山候选搜索", "WebSearch", "高德地图"}, sources)
        self.assertIn("数据检索", result["routeSummary"]["headline"])

    def test_route_summary_includes_planner_and_downstream_agent(self) -> None:
        result = analyze_navi_flow(
            [
                {"_msg": "LLM.DOWN.REQUEST: sub_agent=multi_poi_navi, websocket_url=wss://example.invalid"},
            ],
            plan_type="multi_poi_navi",
        )
        self.assertEqual(
            ["上游未传 FC", "Planner 分类", "下发子 Agent"],
            [node["title"] for node in result["routeSummary"]["nodes"]],
        )

    def test_extracts_latest_previous_turn_result(self) -> None:
        result = analyze_navi_flow(
            [
                {
                    "_msg": (
                        '上游请求内容: {"session":{"dialogHistory":['
                        '{"timestamp":100,"recordId":"old","input":"旧问题",'
                        '"output":{"widget":{"displayText":"旧结果","plan":"simple_navi"}}},'
                        '{"timestamp":200,"recordId":"previous-record","input":"上轮问题",'
                        '"task":"Map","skill":"物模型地图",'
                        '"functions":{"request":{"name":"searchPOI"}},'
                        '"output":{"widget":{"displayText":"上轮最终结果","plan":"simple_navi",'
                        '"streamType":"done","llmOutputType":"simple","needsUserInput":true,'
                        '"content":[{"title":"A"},{"title":"B"}]},"isAlreadyResponse":true}}]}}'
                    )
                }
            ]
        )
        previous = result["previousTurn"]
        self.assertEqual("previous-record", previous["recordId"])
        self.assertEqual("上轮问题", previous["input"])
        self.assertEqual("上轮最终结果", previous["displayText"])
        self.assertEqual("simple_navi", previous["plan"])
        self.assertEqual(2, previous["candidateCount"])
        self.assertTrue(previous["needsUserInput"])
        self.assertTrue(previous["isAlreadyResponse"])

    def test_previous_turn_is_none_without_dialog_history(self) -> None:
        result = analyze_navi_flow([{"_msg": "收到导航请求"}])
        self.assertIsNone(result["previousTurn"])

    def test_previous_turn_stays_before_current_record_timestamp(self) -> None:
        result = analyze_navi_flow(
            [
                {
                    "_msg": (
                        '上游请求内容: {"session":{"dialogHistory":['
                        '{"timestamp":1788420057,"recordId":"20f8478d95aa4d00b8c67389ce78f18a:76a3e4f6a1564080b3019d1788420057:95b4dc2631504da1b342dc1788420057",'
                        '"input":"上轮", "output":{"widget":{"displayText":"上轮结果","plan":"simple_navi"}}},'
                        '{"timestamp":1788420307,"recordId":"18d4b3b2a2164801975c0e51634899fb:6c7e6950399c4c048d192e1788420307:7bb01b26743a4ac28ca9401788420307",'
                        '"input":"后续轮", "output":{"widget":{"displayText":"后续结果","plan":"simple_navi"}}}'
                        ']}}'
                    )
                }
            ],
            current_record_id="50ee29e1f2044c91874f5a631b4118ab:514feffdf70e49f2960e891788420070:5b3f19874e2444058334cf1788420070",
        )
        self.assertEqual("20f8478d95aa4d00b8c67389ce78f18a:76a3e4f6a1564080b3019d1788420057:95b4dc2631504da1b342dc1788420057", result["previousTurn"]["recordId"])

    def test_history_window_returns_two_sides_of_current_turn(self) -> None:
        payload = {
            "session": {
                "dialogHistory": [
                    {"timestamp": 1788420001, "recordId": "before-1", "output": {"widget": {"plan": "p1"}}},
                    {"timestamp": 1788420002, "recordId": "before-2", "output": {"widget": {"plan": "p2"}}},
                    {"timestamp": 1788420004, "recordId": "after-1", "output": {"widget": {"plan": "p4"}}},
                    {"timestamp": 1788420005, "recordId": "after-2", "output": {"widget": {"plan": "p5"}}},
                    {"timestamp": 1788420006, "recordId": "after-3", "output": {"widget": {"plan": "p6"}}},
                ]
            }
        }
        result = analyze_navi_flow(
            [{"_msg": "涓婃父璇锋眰鍐呭: " + __import__("json").dumps(payload)}],
            current_record_id="current:1788420003",
        )
        self.assertEqual(["before-1", "before-2"], [item["recordId"] for item in result["historyBefore"]])
        self.assertEqual(["after-1", "after-2"], [item["recordId"] for item in result["historyAfter"]])

    def test_extracts_tsm_function_and_last_mcp(self) -> None:
        result = analyze_navi_flow(
            [
                {"_msg": '鏀跺埌鍘熷input: {"inputs":[{"functions":{"request":{"name":"mapDeep"}}}]}'},
                {"_msg": "鎵ц宸ュ叿璋冪敤: maps_text_search({})"},
                {"_msg": "鎵ц宸ュ叿璋冪敤: navigateToAddress({})"},
            ]
        )
        self.assertEqual("mapDeep", result["tsmFunction"])
        self.assertIn("navigateToAddress", result["lastMcp"])

    def test_extracts_naviinfo_before_input_and_key_events(self) -> None:
        entries = [
            {
                "_time": "2026-09-03T01:00:00Z",
                "pod_project": "naviag-dev",
                "_msg": (
                    'LLM.UP.REQUEST: {"context":{"system":{"settings":{"naviinfo":'
                    '{"isNaving":true,"currentLocation":{"city":"苏州市"},'
                    '"destination":{"name":"苏州中心"},"waypoints":[]}}}}}'
                ),
            },
            {
                "_time": "2026-09-03T01:00:01Z",
                "pod_project": "naviag-dev",
                "_msg": (
                    '[rid] 收到原始input: {"inputs":[{"input":"导航去苏州中心",'
                    '"source":"aidui","functions":{"request":{"name":"mapDeep",'
                    '"params":[]}}}]}'
                ),
            },
            {
                "_time": "2026-09-03T01:00:02Z",
                "pod_project": "naviag-dev",
                "_msg": "2026-09-03 09:00:02,000 - __main__ - INFO - [rid] 请求函数: mapDeep",
            },
            {
                "_time": "2026-09-03T01:00:03Z",
                "pod_project": "naviag-dev",
                "_msg": "2026-09-03 09:00:03,000 - __main__ - INFO - [rid] 执行工具调用: navigateToAddress({})",
            },
        ]
        result = analyze_navi_flow(entries)
        self.assertTrue(result["naviInfo"]["isNaving"])
        self.assertEqual("苏州中心", result["naviInfo"]["destination"]["name"])
        self.assertEqual("导航去苏州中心", result["input"]["text"])
        self.assertEqual("mapDeep", result["input"]["function"])
        self.assertEqual(["入口函数", "工具调用"], [item["title"] for item in result["events"]])

    def test_ignores_history_naviinfo_when_current_context_exists(self) -> None:
        result = analyze_navi_flow(
            [
                {
                    "_msg": (
                        'LLM.UP.REQUEST: {"dialogHistory":[{"naviinfo":{"destination":{"name":"旧地点"}}}],'
                        '"context":{"system":{"settings":{"naviinfo":{"destination":{"name":"新地点"}}}}}}'
                    )
                }
            ]
        )
        self.assertEqual("新地点", result["naviInfo"]["destination"]["name"])

    def test_extracts_naviinfo_from_upstream_request_marker(self) -> None:
        result = analyze_navi_flow(
            [
                {
                    "_msg": (
                        '上游请求内容: {"context":{"system":{"settings":{"naviinfo":'
                        '{"isNaving":false,"currentLocation":{"city":"北京市"}}}}}}'
                    )
                }
            ]
        )
        self.assertFalse(result["naviInfo"]["isNaving"])
        self.assertEqual("北京市", result["naviInfo"]["currentLocation"]["city"])

    def test_does_not_treat_intermediate_mcp_result_as_final_output(self) -> None:
        result = analyze_navi_flow(
            [{"_msg": "MCP 返回结果: {'result': {'content': ['intermediate']}}"}]
        )
        self.assertEqual("", result["finalOutput"])

    def test_extracts_done_display_text_as_final_output(self) -> None:
        result = analyze_navi_flow(
            [
                {
                    "_msg": (
                        '发送响应: {"response":{"widget":{"streamType":"done",'
                        '"displayText":"已开始导航去苏州中心"}}}'
                    )
                }
            ]
        )
        self.assertEqual("已开始导航去苏州中心", result["finalOutput"])

    def test_skips_large_request_payload_from_key_events(self) -> None:
        result = analyze_navi_flow(
            [
                {"_msg": "[Provider] REQUEST_PAYLOAD: semantic_loop prompt"},
                {"_msg": "KEYLOG.NAVI.STAGE1.RESULT: skill_type=travel"},
            ]
        )
        self.assertEqual(1, len(result["events"]))
        self.assertEqual("Stage1 分类", result["events"][0]["title"])


if __name__ == "__main__":
    unittest.main()
