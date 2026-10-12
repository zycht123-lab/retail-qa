"""Synthetic retail data and the local query/approval boundary."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


# These are DEMO personas, not authentication. Production must use verified identity.
PERSONAS = {
    "alice": {"role": "business", "sites": ("US",)},
    "bob": {"role": "business", "sites": ("JP",)},
    "analyst": {"role": "analyst", "sites": ("US", "JP")},
}

ALLOWED_FUNCTIONS = {"sum", "count", "avg", "min", "max", "round", "abs", "coalesce", "date", "strftime"}
ALLOWED_COLUMNS = {"site_id", "business_date", "net_sales_usd", "paid_orders", "visits", "available_items"}


def require_persona(name: str) -> dict[str, Any]:
    if name not in PERSONAS:
        raise ValueError(f"未知演示身份 {name}；可选：{', '.join(PERSONAS)}")
    return PERSONAS[name]


def init_db(path: Path, business_timezone: str = "America/New_York") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS sales_daily (
            site_id TEXT NOT NULL, business_date TEXT NOT NULL,
            net_sales_usd REAL NOT NULL, paid_orders INTEGER NOT NULL,
            visits INTEGER NOT NULL, available_items INTEGER NOT NULL,
            PRIMARY KEY (site_id, business_date))""")
        db.execute("""CREATE TABLE IF NOT EXISTS query_requests (
            id TEXT PRIMARY KEY, requester TEXT NOT NULL, question TEXT NOT NULL,
            sql_text TEXT NOT NULL, params_json TEXT NOT NULL, reason TEXT NOT NULL,
            digest TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            reviewer TEXT, result_json TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS query_revisions (
            request_id TEXT NOT NULL, old_digest TEXT NOT NULL, new_digest TEXT NOT NULL,
            reviewer TEXT NOT NULL, revised_at TEXT NOT NULL)""")
        count = db.execute("SELECT COUNT(*) FROM sales_daily").fetchone()[0]
        if count:
            return
        today = datetime.now(ZoneInfo(business_timezone)).date()
        rows = []
        for offset in range(60, 0, -1):
            day = today - timedelta(days=offset)
            for site in ("US", "JP"):
                base = 12000 if site == "US" else 8000
                weekday = 1.25 if day.weekday() in (5, 6) else 1.0
                # A visible synthetic dip in recent US data for comparison demos.
                dip = 0.78 if site == "US" and offset <= 12 else 1.0
                orders = round((base / 100) * weekday * dip + (offset % 4) * 3)
                visits = round(orders * (12 if site == "US" else 10) + (offset % 5) * 15)
                sales = round(orders * (83 if site == "US" else 75), 2)
                stock = round((600 if site == "US" else 450) - (40 if dip < 1 else 0) + offset % 9)
                rows.append((site, day.isoformat(), sales, orders, visits, stock))
        db.executemany("INSERT INTO sales_daily VALUES (?, ?, ?, ?, ?, ?)", rows)


def _visible_connection(path: Path, persona: str, *, include_rows: bool = True) -> sqlite3.Connection:
    sites = require_persona(persona)["sites"]
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE sales_daily (site_id TEXT, business_date TEXT, net_sales_usd REAL, paid_orders INTEGER, visits INTEGER, available_items INTEGER)")
    if include_rows:
        with sqlite3.connect(path) as source:
            marks = ",".join("?" for _ in sites)
            rows = source.execute(
                f"SELECT site_id, business_date, net_sales_usd, paid_orders, visits, available_items FROM sales_daily WHERE site_id IN ({marks})",
                sites,
            ).fetchall()
        db.executemany("INSERT INTO sales_daily VALUES (?, ?, ?, ?, ?, ?)", rows)
    db.commit()
    db.execute("PRAGMA query_only = ON")

    def authorize(action: int, arg1: str | None, arg2: str | None, _database: str | None, _source: str | None) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and arg1 == "sales_daily" and arg2 in ALLOWED_COLUMNS | {""}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in ALLOWED_FUNCTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    db.set_authorizer(authorize)
    db.set_progress_handler(lambda: 1, 100_000)
    return db


def run_readonly_query(path: Path, persona: str, sql_text: str, params: dict[str, Any], max_rows: int = 100) -> list[dict[str, Any]]:
    _check_query(sql_text, params, max_rows)
    db = _visible_connection(path, persona)
    try:
        db.execute("EXPLAIN QUERY PLAN " + sql_text, params).fetchall()
        cursor = db.execute(sql_text, params)
        rows = cursor.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            raise ValueError(f"结果超过 {max_rows} 行；请缩小查询范围")
        return [dict(row) for row in rows]
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"SQL 校验或执行失败：{exc}") from exc
    finally:
        db.close()


def _check_query(sql_text: str, params: dict[str, Any], max_rows: int) -> None:
    if not re.match(r"^\s*SELECT\b", sql_text, re.IGNORECASE):
        raise ValueError("只允许单条 SELECT 查询")
    if ";" in sql_text or "--" in sql_text or "/*" in sql_text:
        raise ValueError("SQL 不允许分号或注释")
    if not isinstance(params, dict) or any(not isinstance(k, str) or not isinstance(v, (str, int, float)) or isinstance(v, bool) for k, v in params.items()):
        raise ValueError("SQL 参数必须是非空的字符串或数字；日期参数需给出明确值")
    if max_rows < 1 or max_rows > 1000:
        raise ValueError("max_rows 必须在 1 到 1000 之间")


def validate_readonly_query(path: Path, persona: str, sql_text: str, params: dict[str, Any], max_rows: int = 100) -> None:
    """Parse and authorize without running the query before analyst approval."""
    _check_query(sql_text, params, max_rows)
    db = _visible_connection(path, persona, include_rows=False)
    try:
        db.execute("EXPLAIN QUERY PLAN " + sql_text, params).fetchall()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"SQL 校验失败：{exc}") from exc
    finally:
        db.close()


def _digest(requester: str, sql_text: str, params: dict[str, Any]) -> str:
    payload = json.dumps({"requester": requester, "sql": sql_text, "params": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def save_pending(path: Path, requester: str, question: str, sql_text: str, params: dict[str, Any], reason: str) -> str:
    require_persona(requester)
    request_id = uuid.uuid4().hex[:12]
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO query_requests (id, requester, question, sql_text, params_json, reason, digest, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (request_id, requester, question, sql_text, json.dumps(params, ensure_ascii=False), reason,
             _digest(requester, sql_text, params), datetime.now(timezone.utc).isoformat()),
        )
    return request_id


def get_request(path: Path, request_id: str, actor: str) -> dict[str, Any]:
    persona = require_persona(actor)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM query_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise ValueError("找不到该查询")
    if actor != row["requester"] and persona["role"] != "analyst":
        raise PermissionError("只能查看自己的查询")
    return dict(row)


def list_pending(path: Path, actor: str) -> list[dict[str, Any]]:
    if require_persona(actor)["role"] != "analyst":
        raise PermissionError("只有演示分析师可以查看待审 SQL")
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT id, requester, question, created_at FROM query_requests WHERE status = 'pending' ORDER BY created_at")]


def approve(path: Path, request_id: str, reviewer: str, max_rows: int = 100) -> list[dict[str, Any]]:
    if require_persona(reviewer)["role"] != "analyst":
        raise PermissionError("只有演示分析师可以批准 SQL")
    row = get_request(path, request_id, reviewer)
    if row["status"] != "pending":
        raise ValueError("该查询已处理")
    if datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"]) > timedelta(hours=24):
        raise ValueError("草稿超过 24 小时，请重新提出")
    params = json.loads(row["params_json"])
    if _digest(row["requester"], row["sql_text"], params) != row["digest"]:
        raise ValueError("SQL 或参数在待审期间发生变化")
    results = run_readonly_query(path, row["requester"], row["sql_text"], params, max_rows)
    with sqlite3.connect(path) as db:
        cursor = db.execute(
            "UPDATE query_requests SET status='approved', reviewer=?, result_json=? WHERE id=? AND status='pending' AND digest=?",
            (reviewer, json.dumps(results, ensure_ascii=False), request_id, row["digest"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("查询状态已变化，请重新审查")
    return results


def revise_pending(path: Path, request_id: str, reviewer: str, new_sql: str,
                   new_params: dict[str, Any], max_rows: int = 100) -> str:
    if require_persona(reviewer)["role"] != "analyst":
        raise PermissionError("只有演示分析师可以修改 SQL 草稿")
    row = get_request(path, request_id, reviewer)
    if row["status"] != "pending":
        raise ValueError("只能修改待审草稿")
    validate_readonly_query(path, row["requester"], new_sql, new_params, max_rows)
    digest = _digest(row["requester"], new_sql, new_params)
    with sqlite3.connect(path) as db:
        cursor = db.execute(
            "UPDATE query_requests SET sql_text=?, params_json=?, digest=? WHERE id=? AND status='pending' AND digest=?",
            (new_sql, json.dumps(new_params, ensure_ascii=False), digest, request_id, row["digest"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("查询状态已变化，请重新审查")
        db.execute("INSERT INTO query_revisions VALUES (?, ?, ?, ?, ?)",
                   (request_id, row["digest"], digest, reviewer, datetime.now(timezone.utc).isoformat()))
    return digest
