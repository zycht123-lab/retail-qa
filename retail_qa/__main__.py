"""Command-line demo for the fixed workflow and analyst review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from openai import APIError

from .agent import RetailQAAgent
from .config import PROJECT_ROOT, load_settings, make_model_client
from .data import PERSONAS, approve, get_request, init_db, list_pending, require_persona, revise_pending


def show(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="模板优先的企业问数 Agent PoC（本地合成数据）")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config.toml")
    parser.add_argument("--model", help="覆盖路由/探索模型：qwen/glm/hy4/deepseek")
    parser.add_argument("--sql-model", help="覆盖长尾 SQL 草稿模型")
    parser.add_argument("--judge-model", help="覆盖答案复核模型")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="生成本地合成零售数据")
    ask = sub.add_parser("ask", help="提出自然语言问题")
    ask.add_argument("question")
    ask.add_argument("--user", choices=["alice", "bob", "analyst"], default="alice")
    pending = sub.add_parser("pending", help="列出待审 SQL")
    pending.add_argument("--user", default="analyst")
    review = sub.add_parser("review", help="查看待审 SQL 与参数")
    review.add_argument("request_id")
    review.add_argument("--user", default="analyst")
    revise = sub.add_parser("revise", help="分析师修改待审 SQL，重新校验但不执行")
    revise.add_argument("request_id")
    revise.add_argument("--sql-file", type=Path, required=True)
    revise.add_argument("--params-file", type=Path, help="JSON 参数文件；省略则保留现有参数")
    revise.add_argument("--user", default="analyst")
    approval = sub.add_parser("approve", help="批准并执行已审 SQL")
    approval.add_argument("request_id")
    approval.add_argument("--user", default="analyst")
    result = sub.add_parser("result", help="请求人查看查询状态及结果")
    result.add_argument("request_id")
    result.add_argument("--user", default="alice")
    args = parser.parse_args()

    try:
        settings = load_settings(args.config)
        db_path = settings["database"]
        init_db(db_path, settings["business_timezone"])
        if args.command == "init":
            show({"database": str(db_path), "personas": {k: list(v["sites"]) for k, v in PERSONAS.items()},
                  "note": "所有数据与身份均为本地演示。"})
        elif args.command == "ask":
            aliases = settings.get("models", {})
            router = make_model_client(settings, args.model or aliases.get("router"))
            role_models = {"router": router,
                           "sql": make_model_client(settings, args.sql_model or aliases.get("sql")),
                           "judge": make_model_client(settings, args.judge_model or aliases.get("judge"))}
            agent = RetailQAAgent(db_path, settings["business_timezone"], int(settings["max_result_rows"]),
                                  *router, role_models=role_models)
            show(agent.ask(args.question, args.user))
        elif args.command == "pending":
            show(list_pending(db_path, args.user))
        elif args.command == "review":
            if require_persona(args.user)["role"] != "analyst":
                raise PermissionError("只有演示分析师可查看 SQL 草稿")
            row = get_request(db_path, args.request_id, args.user)
            show({k: row[k] for k in ("id", "requester", "question", "sql_text", "params_json", "reason", "digest", "status")})
        elif args.command == "revise":
            row = get_request(db_path, args.request_id, args.user)
            params = (json.loads(args.params_file.read_text()) if args.params_file
                      else json.loads(row["params_json"]))
            digest = revise_pending(db_path, args.request_id, args.user,
                                    args.sql_file.read_text(), params, int(settings["max_result_rows"]))
            show({"status": "pending", "request_id": args.request_id, "new_digest": digest,
                  "message": "已重新校验但尚未执行；请重新 review 后再 approve。"})
        elif args.command == "approve":
            rows = approve(db_path, args.request_id, args.user, int(settings["max_result_rows"]))
            show({"status": "approved", "request_id": args.request_id, "rows": rows,
                  "note": "查询按请求人的可见站点执行；数据仍是本地合成演示数据。"})
        elif args.command == "result":
            row = get_request(db_path, args.request_id, args.user)
            show({"status": row["status"], "request_id": row["id"],
                  "rows": json.loads(row["result_json"]) if row["result_json"] else None})
        return 0
    except APIError as exc:
        status = getattr(exc, "status_code", None)
        print(f"模型请求失败（HTTP {status or '连接错误'}）。请检查模型、Key、额度和网络。", file=sys.stderr)
        return 1
    except (ValueError, PermissionError, OSError, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
