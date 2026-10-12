"""Call a real configured model and score its routing on labeled questions."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from retail_qa.agent import RetailQAAgent
from retail_qa.config import load_settings, make_model_client


HERE = Path(__file__).resolve().parent


def evaluate(case: dict, settings: dict, alias: str) -> dict:
    try:
        client, model = make_model_client(settings, alias)
        agent = RetailQAAgent(Path("/dev/null"), settings["business_timezone"],
                              int(settings["max_result_rows"]), client, model)
        decision = agent.route(case["question"], case["user"])
        actual = decision.get("action")
        passed = actual == case["expected_action"]
        if actual == "clarify":
            passed = passed and bool(str(decision.get("message") or "").strip())
        return {**case, "actual_action": actual, "passed": passed,
                "decision": decision, "usage": agent.usage, "model": model["id"]}
    except Exception as exc:
        return {**case, "actual_action": "ERROR", "passed": False, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="真实模型路由评测：不访问数据库")
    parser.add_argument("--model", default=None, help="模型别名；默认 config.toml 的 router")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", type=Path, default=HERE / "latest_results.json")
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        parser.error("--workers 必须在 1 到 4 之间")
    settings = load_settings()
    alias = args.model or settings["models"]["router"]
    cases = json.loads((HERE / "router_cases.json").read_text(encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda case: evaluate(case, settings, alias), cases))
    summary = {"model_alias": alias, "total": len(results),
               "passed": sum(bool(item["passed"]) for item in results),
               "ambiguous_total": sum(item["expected_action"] == "clarify" for item in results),
               "ambiguous_passed": sum(item["expected_action"] == "clarify" and item["passed"] for item in results),
               "results": results}
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for item in results:
        mark = "✓" if item["passed"] else "✗"
        print(f"{mark} {item['id']}: {item['question']} | 预期 {item['expected_action']} / 实际 {item['actual_action']}")
        if not item["passed"]:
            print(f"  标注理由：{item['why']}；模型答复：{item.get('decision', item.get('error'))}")
    print(f"总计 {summary['passed']}/{summary['total']}；含糊问题 {summary['ambiguous_passed']}/{summary['ambiguous_total']}")
    print(f"详细结果：{args.output}")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
