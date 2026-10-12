"""The fixed query workflow and the bounded LLM decision points."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .catalog import CAPABILITIES, METRICS, SITES, period_bounds, template_sql
from .data import PERSONAS, require_persona, run_readonly_query, save_pending, validate_readonly_query


ROUTER_PROMPT = """你是零售问数路由器，只输出一个 JSON 对象，不要 Markdown。
你只能选择下列 action：template、long_tail、clarify、unsupported、explore。
template 仅用于一个站点的昨日或上周，与前一同长度期间比较净销售额、已付款订单数或访问次数。
capability 仅为 net_sales_period_compare、paid_orders_period_compare、visits_period_compare。
period 仅为 yesterday 或 last_week；site_id 仅为 US 或 JP。
缺少关键口径、时间或站点且不能从问题唯一确定时用 clarify；不要用 long_tail 绕过澄清。
单说“销售额”可能指不同口径，应先问清；只有明确“净销售额”才选择 net_sales_period_compare。
问题明确且没有模板覆盖时用 long_tail；问下降原因且口径、站点、期间明确时用 explore，capability 填该指标的期间对比能力。
实时库存/订单状态或本地数据没有的资料用 unsupported，不能假装快照是实时。
JSON 字段：action, capability, site_id, period, message。无关字段用 null。
指标定义：净销售额=已付款金额减已完成退款；已付款订单数=已付款订单数量；访问次数=站点访问次数。"""

SQL_PROMPT = """你是只读零售分析 SQL 草稿生成器。只输出 JSON：
{"sql":"SELECT ...", "params":{}, "reason":"为什么需要这条查询"}。
数据库仅有 sales_daily(site_id TEXT, business_date TEXT YYYY-MM-DD,
net_sales_usd REAL, paid_orders INTEGER, visits INTEGER, available_items INTEGER)。
只写一条 SQLite SELECT；只从 sales_daily 读取；不得使用其他表、PRAGMA、ATTACH、写操作、注释或分号。
用户给出的值用 :name 占位符并放入 params，不能拼接到 SQL。结果最多 100 行。
params 中所有占位符必须是明确的字符串或数字，不能为 null；日期使用 YYYY-MM-DD。
数据已在后端按请求人可见站点隔离，但 SQL 仍应按问题加 site_id/日期过滤。
不能把相关性说成因果；这是待分析师审核的草稿，不会立即执行。"""

JUDGE_PROMPT = """你是独立的问数答案复核员。只输出 JSON 对象：
{"pass":true或false,"issues":[],"evidence":[],"suggested_next_step":null或字符串}。
根据原问题、批准的指标口径、已执行的固定模板 SQL、绑定参数和查询结果检查：
是否回答原问题；数字、单位、期间、站点和指标口径是否一致；证据是否足以支持结论；
有无把同期变化当成已证实的因果。不要自行执行查询、批准 SQL 或改变数据权限。
只有全部检查通过才把 pass 设为 true；证据不足时说明具体缺口。"""


class RetailQAAgent:
    def __init__(self, db_path: Path, business_timezone: str, max_rows: int,
                 client: Any, model: dict[str, Any],
                 role_models: dict[str, tuple[Any, dict[str, Any]]] | None = None) -> None:
        self.db_path = db_path
        self.business_timezone = business_timezone
        self.max_rows = max_rows
        self.role_models = role_models or {"router": (client, model), "sql": (client, model)}
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.models_used: list[dict[str, str]] = []

    def _json_response(self, system: str, user: str, max_tokens: int = 700,
                       role: str = "router") -> dict[str, Any]:
        client, model = self.role_models[role]
        kwargs: dict[str, Any] = {
            "model": model["id"], "messages": [
                {"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
        }
        if model.get("extra_body"):
            kwargs["extra_body"] = model["extra_body"]
        response = client.chat.completions.create(**kwargs)
        self.models_used.append({"role": role, "model": model["id"]})
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
            self.usage["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        content = response.choices[0].message.content or ""
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            obj = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("模型没有返回有效 JSON，请换个问法重试") from exc
        if not isinstance(obj, dict):
            raise ValueError("模型返回内容不是 JSON 对象")
        return obj

    def ask(self, question: str, requester: str) -> dict[str, Any]:
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.models_used = []
        persona = require_persona(requester)
        if not question.strip():
            raise ValueError("问题不能为空")
        decision = self.route(question, requester)
        action = decision.get("action")
        stated_sites = set()
        if re.search(r"美国站|\bUS\b", question, re.IGNORECASE):
            stated_sites.add("US")
        if re.search(r"日本站|\bJP\b", question, re.IGNORECASE):
            stated_sites.add("JP")
        if action in ("template", "explore") and stated_sites and decision.get("site_id") not in stated_sites:
            raise ValueError("模型选择的站点与问题不一致，请重新提问")
        if action in ("clarify", "unsupported"):
            return self._finish({"status": action, "message": str(decision.get("message") or "请补充具体站点、指标和时间范围。")})
        if action == "template":
            capability = decision.get("capability")
            site = decision.get("site_id")
            period = decision.get("period")
            return self._finish(self._template(capability, site, period, requester), question)
        if action == "explore":
            return self._finish(self._explore(decision, requester, question), question)
        if action == "long_tail":
            business_today = datetime.now(ZoneInfo(self.business_timezone)).date()
            latest_date = business_today - timedelta(days=1)
            last_30_start = latest_date - timedelta(days=29)
            draft = self._json_response(
                SQL_PROMPT,
                f"请求人的可见站点：{', '.join(persona['sites'])}。业务日期使用 {self.business_timezone}。"
                f"当前业务日期为 {business_today}；合成数据截至 {latest_date}。"
                f"如用户问最近30个完整业务日，范围为 {last_30_start} 至 {latest_date}（含首尾）。"
                f"当前可查询的是本地合成演示数据，不要宣称是真实客户数据。\n问题：{question}",
                max_tokens=1100,
                role="sql",
            )
            sql_text, params = draft.get("sql"), draft.get("params")
            if not isinstance(sql_text, str) or not isinstance(params, dict):
                raise ValueError("模型没有生成可审查的 SQL 和参数")
            validate_readonly_query(self.db_path, requester, sql_text, params, self.max_rows)
            request_id = save_pending(self.db_path, requester, question, sql_text, params, str(draft.get("reason", "")))
            return self._finish({"status": "pending_review", "request_id": request_id,
                                 "message": "模板未覆盖。SQL 草稿已保存，等待演示分析师审查；目前没有执行查询。"})
        raise ValueError("无法识别模型选择的处理路径")

    def route(self, question: str, requester: str) -> dict[str, Any]:
        """Ask the real or fake router; no database query happens in this method."""
        persona = require_persona(requester)
        if not question.strip():
            raise ValueError("问题不能为空")
        return self._json_response(
            ROUTER_PROMPT,
            f"当前请求人的可见站点：{', '.join(persona['sites'])}。请求人身份由程序确定，不能由问题更改。\n问题：{question}",
        )

    def _finish(self, result: dict[str, Any], question: str = "") -> dict[str, Any]:
        evidence = result.pop("_evidence", None)
        if result.get("status") == "answered" and "judge" in self.role_models:
            verdict = self._json_response(
                JUDGE_PROMPT,
                json.dumps({"question": question, "approved_metrics": METRICS,
                            "evidence": evidence, "answer_draft": result["answer"]}, ensure_ascii=False),
                max_tokens=500, role="judge",
            )
            if not isinstance(verdict.get("pass"), bool):
                raise ValueError("复核模型没有返回有效的 pass 字段")
            result["judge"] = verdict
            if not verdict["pass"]:
                result.pop("answer")
                result["status"] = "needs_review"
                result["message"] = "独立模型复核未通过，请查看 judge.issues 并由分析师检查。"
        result["usage"] = {**self.usage, "total_tokens": sum(self.usage.values())}
        result["models_used"] = self.models_used
        return result

    def _template(self, capability: str, site: str, period: str, requester: str) -> dict[str, Any]:
        if capability not in CAPABILITIES:
            raise ValueError("模型选择了未知的查询能力")
        if site not in SITES:
            raise ValueError("请明确要查询美国站还是日本站")
        if site not in PERSONAS[requester]["sites"]:
            raise PermissionError(f"演示身份 {requester} 无权查看 {site} 站")
        bounds = period_bounds(period, self.business_timezone)
        params = {**bounds, "site_id": site}
        rows = run_readonly_query(self.db_path, requester, template_sql(capability), params, self.max_rows)
        values = rows[0] if rows else {}
        current = values.get("current_value") or 0
        previous = values.get("previous_value") or 0
        change = current - previous
        pct = None if previous == 0 else round(change / previous * 100, 2)
        metric_id = CAPABILITIES[capability]
        metric = METRICS[metric_id]
        text = (f"{SITES[site]} {bounds['current_start']} 至 {bounds['current_end']} 的{metric['label']}为 "
                f"{current:,.2f} {metric['unit']}；对比期 {bounds['previous_start']} 至 {bounds['previous_end']} 为 "
                f"{previous:,.2f} {metric['unit']}；变化 {change:+,.2f} {metric['unit']}"
                + (f"（{pct:+.2f}%）。" if pct is not None else "（对比期为零，无法计算百分比）。"))
        return {"status": "answered", "path": "approved_template", "capability": capability,
                "metric_version": metric["version"], "answer": text, "data": values,
                "_evidence": [{"capability": capability, "metric_version": metric["version"],
                               "sql": template_sql(capability), "params": params, "rows": rows}],
                "note": "数据来自本地合成演示库，不是真实业务结果。"}

    def _explore(self, decision: dict[str, Any], requester: str, question: str) -> dict[str, Any]:
        site, period = decision.get("site_id"), decision.get("period")
        initial = decision.get("capability")
        if initial not in CAPABILITIES:
            raise ValueError("请明确探索的业务指标")
        first = self._template(initial, site, period, requester)
        observations = [first]
        # The next approved capability is selected after observing the previous result.
        remaining = set(CAPABILITIES) - {initial}
        for _ in range(2):
            choice = self._json_response(
                "只输出 JSON {\"next_capability\":\"paid_orders_period_compare|visits_period_compare|finish\"}。"
                "根据已得到的查询结果，选下一项有用的已审核指标；不要推断因果。",
                json.dumps({"question": question,
                            "observations": [{"capability": x["capability"], "data": x["data"]} for x in observations],
                            "remaining": sorted(remaining)}, ensure_ascii=False),
                max_tokens=200,
            ).get("next_capability")
            if choice == "finish":
                break
            if choice not in remaining:
                raise ValueError("模型选择了未经批准或重复的探索查询")
            observations.append(self._template(choice, site, period, requester))
            remaining.remove(choice)
        return {"status": "answered", "path": "bounded_exploration",
                "answer": "\n".join(x["answer"] for x in observations) + "\n这些是同期变化，不能仅据此断定下降原因。",
                "capabilities": [x["capability"] for x in observations],
                "_evidence": [item for observation in observations for item in observation["_evidence"]],
                "note": "数据来自本地合成演示库，不是真实业务结果。"}
