# -*- coding: utf-8 -*-
"""本地 SQLite 增量入库 — 资金流水 / 贴现流水 / 背书流水。

设计（与老师项目 core/db.py 同思路，标准库 sqlite3，零新依赖）：
  · 单文件 output/bank_data.db；
  · 按业务唯一键 INSERT OR REPLACE，重复同步不产生重复行；
  · 每张表存「原始数据」列（抓到的整行 JSON），字段调整不丢数据；
  · 表名、列名全用中文，方便直接在 Navicat 里看；带斜杠的列名用双引号包裹。

唯一键（唯一标识）：
  资金流水    查询账号+日期+金额+对方户名+摘要 算出的指纹
  贴现流水    查询账号+票据号+子票区间
  背书流水    查询账号+票据号+交易流水号
  账号纳入唯一键：多个公司U盾的同一张票/同一笔收付不会互相覆盖。

多账号：upsert(kind, records, account_no=...) 传入本次查询的账号；
        不传时保持旧算法，兼容已有测试数据。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from config.settings import BASE_DIR

DB_PATH = Path(BASE_DIR) / "output" / "bank_data.db"

# 表名 -> 建表语句（列名全中文）
_DDL: dict[str, str] = {
    "资金流水": """
        CREATE TABLE IF NOT EXISTS "资金流水" (
            "唯一标识"      TEXT PRIMARY KEY,
            "查询账号"      TEXT,
            "账/卡号"       TEXT,
            "交易日期"      TEXT,
            "交易渠道"      TEXT,
            "币种"          TEXT,
            "收入支出项"    TEXT,
            "余额"          TEXT,
            "凭证号"        TEXT,
            "对方银行名称"  TEXT,
            "对方账/卡号"   TEXT,
            "对方户名"      TEXT,
            "摘要"          TEXT,
            "原始数据"      TEXT,
            "同步时间"      TEXT
        )
    """,
    "贴现流水": """
        CREATE TABLE IF NOT EXISTS "贴现流水" (
            "唯一标识"      TEXT PRIMARY KEY,
            "查询账号"      TEXT,
            "票据号码"      TEXT,
            "子票区间"      TEXT,
            "票面金额"      TEXT,
            "业务日期"      TEXT,
            "原始数据"      TEXT,
            "同步时间"      TEXT
        )
    """,
    "背书流水": """
        CREATE TABLE IF NOT EXISTS "背书流水" (
            "唯一标识"      TEXT PRIMARY KEY,
            "查询账号"      TEXT,
            "票据号码"      TEXT,
            "子票区间"      TEXT,
            "票面金额"      TEXT,
            "交易日期"      TEXT,
            "业务类型"      TEXT,
            "原始数据"      TEXT,
            "同步时间"      TEXT
        )
    """,
}

# 每张表要写入的列（顺序与 _normalize 返回一致）
_COLS: dict[str, list[str]] = {
    "资金流水": ["唯一标识", "查询账号", "账/卡号", "交易日期", "交易渠道", "币种", "收入支出项", "余额",
             "凭证号", "对方银行名称", "对方账/卡号", "对方户名", "摘要", "原始数据", "同步时间"],
    "贴现流水": ["唯一标识", "查询账号", "票据号码", "子票区间", "票面金额", "业务日期", "原始数据", "同步时间"],
    "背书流水": ["唯一标识", "查询账号", "票据号码", "子票区间", "票面金额", "交易日期", "业务类型", "原始数据", "同步时间"],
}

# 兼容旧调用方传的英文名（类型名 -> 中文表名）
TABLE_OF_KIND = {
    "资金流水": "资金流水",
    "贴现流水": "贴现流水",
    "背书流水": "背书流水",
    "fund_flow": "资金流水",
    "discount_flow": "贴现流水",
    "endorse_flow": "背书流水",
}


def _q(name: str) -> str:
    """SQL 标识符加双引号（列名含 / 时必须）。"""
    return '"' + name.replace('"', '""') + '"'


def _table(kind: str) -> str:
    table = TABLE_OF_KIND.get(kind)
    if not table:
        raise ValueError(f"未知数据类型：{kind}")
    return table


def _ensure_column(con: sqlite3.Connection, table: str, column: str, decl: str = "TEXT") -> None:
    """旧表缺列时补列（ALTER TABLE，不删表、不丢数据）。"""
    cols = {r[1] for r in con.execute(f'PRAGMA table_info({_q(table)})')}
    if column not in cols:
        con.execute(f'ALTER TABLE {_q(table)} ADD COLUMN {_q(column)} {decl}')


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    for ddl in _DDL.values():
        con.execute(ddl)
    # 兼容旧库：三张表补上「查询账号」列
    for table in ("资金流水", "贴现流水", "背书流水"):
        _ensure_column(con, table, "查询账号")
    con.commit()
    return con


def _fingerprint(*parts: Any) -> str:
    raw = "|".join(str(p or "").strip() for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _get(rec: dict[str, Any], *names: str) -> str:
    """按多个候选表头取第一个非空值（各行措辞略有差异时兜底）。"""
    for n in names:
        v = rec.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _normalize(kind: str, rec: dict[str, Any], account_no: str = "") -> dict[str, Any]:
    """把抓数出来的记录归一成中文入库列。

    account_no 是本条数据所属的查询账号，纳入唯一标识，
    这样两家公司的同一张票/同一笔收付不会互相覆盖。
    """
    table = _table(kind)
    acct_hint = str(account_no or "").strip()
    if table == "资金流水":
        acct = _get(rec, "账/卡号", "账号") or acct_hint
        ttime = _get(rec, "交易日期", "交易时间")
        amount = _get(rec, "收入支出项", "金额")
        other = _get(rec, "对方户名")
        summary = _get(rec, "摘要")
        return {
            "唯一标识": _fingerprint(acct_hint, acct, ttime, amount, other, summary),
            "查询账号": acct_hint,
            "账/卡号": acct,
            "交易日期": ttime,
            "交易渠道": _get(rec, "交易渠道"),
            "币种": _get(rec, "币种"),
            "收入支出项": amount,
            "余额": _get(rec, "余额"),
            "凭证号": _get(rec, "凭证号"),
            "对方银行名称": _get(rec, "对方银行名称"),
            "对方账/卡号": _get(rec, "对方账/卡号", "对方账号"),
            "对方户名": other,
            "摘要": summary,
        }
    if table == "贴现流水":
        bill = _get(rec, "票据号码", "票据号", "票号")
        rng = _get(rec, "子票区间", "票据区间")
        uid = f"{acct_hint}#{bill}#{rng}" if (bill or rng) else _fingerprint(acct_hint, *[str(v) for v in rec.values()])
        return {
            "唯一标识": uid,
            "查询账号": acct_hint,
            "票据号码": bill,
            "子票区间": rng,
            "票面金额": _get(rec, "票面金额", "票据金额", "金额"),
            "业务日期": _get(rec, "贴现日期", "交易日期", "业务日期"),
        }
    # 背书流水
    bill = _get(rec, "票据号码", "票据号", "票号")
    serial = _get(rec, "交易流水号", "流水号", "业务流水号")
    uid = f"{acct_hint}#{bill}#{serial}" if (bill or serial) else _fingerprint(acct_hint, *[str(v) for v in rec.values()])
    return {
        "唯一标识": uid,
        "查询账号": acct_hint,
        "票据号码": bill,
        "子票区间": _get(rec, "子票区间", "票据区间"),
        "票面金额": _get(rec, "票面金额", "票据金额", "金额"),
        "交易日期": _get(rec, "交易日期", "交易时间"),
        "业务类型": _get(rec, "业务类型", "交易类型"),
    }


def upsert(kind: str, records: Iterable[dict[str, Any]], account_no: str = "") -> int:
    """增量入库，返回实际写入（新增+覆盖）行数。

    account_no：本次查询所属账号，纳入唯一键；不传则兼容旧测试数据。
    """
    table = _table(kind)
    recs = list(records)
    if not recs:
        return 0
    cols = _COLS[table]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for rec in recs:
        row = _normalize(table, rec, account_no)
        row["原始数据"] = json.dumps(rec, ensure_ascii=False)
        row["同步时间"] = now
        rows.append(tuple(row.get(c) for c in cols))
    quoted = ",".join(_q(c) for c in cols)
    ph = ",".join("?" * len(cols))
    con = _connect()
    try:
        before = con.total_changes
        con.executemany(
            f'INSERT OR REPLACE INTO {_q(table)} ({quoted}) VALUES ({ph})',
            rows,
        )
        con.commit()
        return con.total_changes - before
    finally:
        con.close()


def count(kind: str) -> int:
    table = _table(kind)
    con = _connect()
    try:
        return int(con.execute(f'SELECT COUNT(*) FROM {_q(table)}').fetchone()[0])
    finally:
        con.close()


def rows(kind: str, limit: int = 100) -> list[dict[str, Any]]:
    table = _table(kind)
    con = _connect()
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            f'SELECT * FROM {_q(table)} ORDER BY rowid DESC LIMIT ?', (limit,)
        ).fetchall()]
    finally:
        con.close()
