import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from retail_qa.agent import RetailQAAgent
from retail_qa.data import approve, get_request, init_db, revise_pending, run_readonly_query, validate_readonly_query


class FakeCompletions:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = json.dumps(self.responses.pop(0), ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class RetailFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "demo.db"
        init_db(self.db)

    def agent(self, *responses):
        completions = FakeCompletions(*responses)
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        return RetailQAAgent(self.db, "America/New_York", 100, client,
                             {"id": "fake-model", "extra_body": {"thinking": {"type": "disabled"}}}), completions

    def test_approved_template_executes_without_generated_sql(self):
        agent, completions = self.agent({
            "action": "template", "capability": "net_sales_period_compare",
            "site_id": "US", "period": "last_week", "message": None,
        })
        answer = agent.ask("美国站上周净销售额比前一周变化多少？", "alice")
        self.assertEqual(answer["status"], "answered")
        self.assertEqual(answer["path"], "approved_template")
        self.assertIn("净销售额", answer["answer"])
        self.assertEqual(len(completions.calls), 1)
        self.assertEqual(completions.calls[0]["extra_body"]["thinking"]["type"], "disabled")

    def test_model_cannot_choose_site_outside_requester_scope(self):
        agent, _ = self.agent({"action": "template", "capability": "visits_period_compare",
                               "site_id": "JP", "period": "yesterday"})
        with self.assertRaises(PermissionError):
            agent.ask("日本站昨天的访问量？", "alice")

    def test_model_cannot_silently_change_explicit_site_for_analyst(self):
        agent, _ = self.agent({"action": "template", "capability": "visits_period_compare",
                               "site_id": "JP", "period": "yesterday"})
        with self.assertRaisesRegex(ValueError, "不一致"):
            agent.ask("美国站昨天的访问量？", "analyst")

    def test_ambiguous_question_does_not_run_a_query(self):
        agent, completions = self.agent({"action": "clarify", "message": "请明确销售额口径和时间。"})
        answer = agent.ask("销售额怎么样？", "alice")
        self.assertEqual(answer["status"], "clarify")
        self.assertEqual(len(completions.calls), 1)

    def test_long_tail_waits_for_review_then_runs_with_requester_scope(self):
        agent, _ = self.agent(
            {"action": "long_tail"},
            {"sql": "SELECT site_id, SUM(net_sales_usd) AS total FROM sales_daily GROUP BY site_id",
             "params": {}, "reason": "按站点汇总"},
        )
        answer = agent.ask("按站点汇总净销售额", "alice")
        self.assertEqual(answer["status"], "pending_review")
        row = get_request(self.db, answer["request_id"], "alice")
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["result_json"])
        with self.assertRaises(PermissionError):
            approve(self.db, answer["request_id"], "bob")
        rows = approve(self.db, answer["request_id"], "analyst")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site_id"], "US")
        self.assertEqual(get_request(self.db, answer["request_id"], "alice")["status"], "approved")
        with self.assertRaises(PermissionError):
            get_request(self.db, answer["request_id"], "bob")

    def test_sql_guard_denies_other_tables_functions_and_multi_statement(self):
        for statement in ["SELECT * FROM sqlite_master", "SELECT load_extension('x')",
                          "SELECT * FROM sales_daily; DROP TABLE sales_daily"]:
            with self.subTest(statement=statement), self.assertRaises(ValueError):
                validate_readonly_query(self.db, "alice", statement, {})
        rows = run_readonly_query(self.db, "bob", "SELECT DISTINCT site_id FROM sales_daily", {})
        self.assertEqual(rows, [{"site_id": "JP"}])

    def test_sql_guard_denies_unresolved_date_parameter(self):
        with self.assertRaisesRegex(ValueError, "日期参数"):
            validate_readonly_query(self.db, "alice",
                                    "SELECT business_date FROM sales_daily WHERE business_date >= :start_date",
                                    {"start_date": None})

    def test_draft_validation_uses_schema_without_opening_data_file(self):
        missing_data_file = Path(self.temp.name) / "not-created.db"
        validate_readonly_query(missing_data_file, "alice", "SELECT COUNT(*) AS n FROM sales_daily", {})
        self.assertFalse(missing_data_file.exists())

    def test_tampered_sql_cannot_be_approved(self):
        agent, _ = self.agent({"action": "long_tail"},
                              {"sql": "SELECT COUNT(*) AS n FROM sales_daily", "params": {}, "reason": "计数"})
        request_id = agent.ask("演示数据有多少日记录？", "alice")["request_id"]
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE query_requests SET sql_text='SELECT 1' WHERE id=?", (request_id,))
        with self.assertRaisesRegex(ValueError, "发生变化"):
            approve(self.db, request_id, "analyst")

    def test_analyst_revision_requires_a_new_explicit_approval(self):
        agent, _ = self.agent({"action": "long_tail"},
                              {"sql": "SELECT COUNT(*) AS n FROM sales_daily", "params": {}, "reason": "计数"})
        request_id = agent.ask("演示数据有多少日记录？", "alice")["request_id"]
        revise_pending(self.db, request_id, "analyst",
                       "SELECT SUM(net_sales_usd) AS total FROM sales_daily WHERE site_id=:site", {"site": "US"})
        self.assertEqual(get_request(self.db, request_id, "alice")["status"], "pending")
        rows = approve(self.db, request_id, "analyst")
        self.assertGreater(rows[0]["total"], 0)

    def test_open_question_uses_bounded_approved_exploration(self):
        agent, completions = self.agent(
            {"action": "explore", "capability": "net_sales_period_compare", "site_id": "US", "period": "last_week"},
            {"next_capability": "paid_orders_period_compare"},
            {"next_capability": "finish"},
        )
        answer = agent.ask("美国站上周净销售额为什么下降？", "alice")
        self.assertEqual(answer["path"], "bounded_exploration")
        self.assertEqual(answer["capabilities"], ["net_sales_period_compare", "paid_orders_period_compare"])
        self.assertIn("不能仅据此断定下降原因", answer["answer"])
        self.assertEqual(len(completions.calls), 3)

    def test_separate_router_sql_and_judge_models(self):
        def role(model_id, *responses):
            completions = FakeCompletions(*responses)
            client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            return (client, {"id": model_id}), completions

        router, router_calls = role("qwen-router", {"action": "template",
            "capability": "net_sales_period_compare", "site_id": "US", "period": "last_week"})
        sql, sql_calls = role("glm-sql")
        judge, judge_calls = role("hy4-judge", {"pass": True, "issues": [],
                                                  "evidence": ["数字与查询结果一致"],
                                                  "suggested_next_step": None})
        agent = RetailQAAgent(self.db, "America/New_York", 100, *router,
                              role_models={"router": router, "sql": sql, "judge": judge})
        answer = agent.ask("美国站上周净销售额比前一周变化多少？", "alice")
        self.assertEqual(answer["status"], "answered")
        self.assertEqual([x["model"] for x in answer["models_used"]], ["qwen-router", "hy4-judge"])
        self.assertEqual(len(router_calls.calls), 1)
        self.assertEqual(len(sql_calls.calls), 0)
        self.assertEqual(len(judge_calls.calls), 1)
        self.assertIn("net_sales_period_compare", judge_calls.calls[0]["messages"][1]["content"])

    def test_judge_failure_withholds_answer(self):
        router = FakeCompletions({"action": "template", "capability": "visits_period_compare",
                                  "site_id": "US", "period": "yesterday"})
        judge = FakeCompletions({"pass": False, "issues": ["证据不足"],
                                 "evidence": [], "suggested_next_step": "人工检查"})
        def client(completions):
            return SimpleNamespace(chat=SimpleNamespace(completions=completions))
        roles = {"router": (client(router), {"id": "router"}),
                 "sql": (client(router), {"id": "sql"}),
                 "judge": (client(judge), {"id": "judge"})}
        agent = RetailQAAgent(self.db, "America/New_York", 100, *roles["router"], role_models=roles)
        answer = agent.ask("美国站昨天访问次数比前一天变化多少？", "alice")
        self.assertEqual(answer["status"], "needs_review")
        self.assertNotIn("answer", answer)
        self.assertEqual(answer["judge"]["issues"], ["证据不足"])


if __name__ == "__main__":
    unittest.main()
