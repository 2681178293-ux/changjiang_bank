# -*- coding: utf-8 -*-
"""本地入库成功后，把长江网银数据映射并推送到飞诺 ERP（参照老师项目协议）。

分层：
    config/feinuo_config.py  飞诺配置读写（分组 A-E ↔ 1-5）
    service/feinuo_api.py   【本文件】字段映射 + POST 推送（业务集成，不依赖 Qt）
    service/sync_service.py 入库成功后调用本模块
    main.py / ui/sync_worker.py 只显示日志

接口与老师项目完全一致：
    POST {baseUrl}/feinuo-api/payment-flow/import        资金流水
    POST {baseUrl}/feinuo-api/discount-repayment/import  贴现流水
    Header: X-Api-Key
    Body:   {"updateSupport": true, "items": [...], "groupName": N}
    响应:   {"code": 200, ...}

字段映射按长江抓到的中文表头做，账号用配置里的完整账号（网银明细是脱敏的）。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

import requests
import urllib3

from config import feinuo_config
from core.query_cjccb import normalize_bill_range

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LogFn = Callable[[str], None]


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)
    else:
        print(msg)


def api_cfg() -> dict[str, Any]:
    """读取推送配置；分组以页面选择为准（feinuo_config 由页面选择即时写回）。"""
    raw = feinuo_config.load_config()
    nested = raw.get("feinuo_api") if isinstance(raw.get("feinuo_api"), dict) else {}
    merged = {**nested, **raw}

    group = feinuo_config.get_group()
    if group is None:
        try:
            group = int(merged.get("group_name", merged.get("groupName")) or 0)
        except (TypeError, ValueError):
            group = 0
    if group not in (1, 2, 3, 4, 5):
        group = 1

    def pick(*keys: str, default: str = "") -> str:
        for k in keys:
            v = merged.get(k)
            if v not in (None, ""):
                return str(v).strip()
        return default

    return {
        "enabled": merged.get("enabled", False) is True,
        "base_url": pick("base_url", "baseUrl", default="https://www.feinuoyun.com/prod-api").rstrip("/"),
        "api_key": pick("api_key", "apiKey"),
        "group_name": group,
        "owner_company": pick("owner_company", "ownerCompany"),
        "source_bank": pick("source_bank", "sourceBank", "defaultSourceBank", default="长江"),
        "source_file": pick("source_file", "sourceFile", default="长江网银工具"),
        "batch_size": max(1, int(merged.get("batch_size") or merged.get("batchSize") or 50)),
        "fail_on_error": bool(merged.get("fail_on_error") or merged.get("failOnError") or False),
        "update_support": merged.get("updateSupport", True) is not False,
        "timeout": float(merged.get("timeoutSeconds") or 60),
    }


# ---------- 清洗工具 ----------
def _get(rec: dict[str, Any], *names: str) -> str:
    """按多个候选中文表头取第一个非空值。"""
    for n in names:
        v = rec.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _to_num(value: Any) -> float | None:
    """' +￥3,727,239.80 ' -> 3727239.8；'￥1,200.00-' 等异常格式尽量兜住。"""
    if value is None:
        return None
    s = str(value).strip()
    for ch in ("￥", "¥", ",", " ", "\xa0"):
        s = s.replace(ch, "")
    if s in ("", "-", "—", "+"):
        return None
    neg = s.endswith("-")          # 少数银行把负号放末尾
    s = s.rstrip("-")
    sign = -1 if neg or s.startswith("-") else 1
    s = s.lstrip("+-")
    try:
        return sign * float(s)
    except ValueError:
        return None


def _compact(item: dict[str, Any]) -> dict[str, Any]:
    """去掉 None / 空串 / 空容器（与老师 _api_item_compact 一致）。"""
    return {
        k: v for k, v in item.items()
        if v is not None
        and not (isinstance(v, str) and not v.strip())
        and not (isinstance(v, (list, dict)) and not v)
    }


# ---------- 字段映射 ----------
def map_payment(
    row: dict[str, Any],
    *,
    account_no: str,
    group_name: int,
    source_bank: str,
    source_file: str,
    owner_company: str = "",
) -> dict[str, Any] | None:
    """长江资金流水 -> 飞诺 payment-flow（打款/流水表）。"""
    if not isinstance(row, dict):
        return None
    ttime = _get(row, "交易日期", "交易时间", "日期")
    if not ttime:
        return None
    amt = _to_num(_get(row, "收入支出项", "金额", "交易金额"))
    # 长江单列带符号：正数=收入(贷方)，负数=支出(借方)
    credit = round(amt, 2) if amt is not None and amt > 0 else 0.0
    debit = round(-amt, 2) if amt is not None and amt < 0 else 0.0
    company = owner_company or _get(row, "本方户名", "账户名称", "户名")
    item = {
        "transactionSerialNo": _get(row, "凭证号", "流水号", "交易流水号"),
        "counterpartyBank": _get(row, "对方银行名称", "对方开户行", "对方行名"),
        "counterpartyAccount": _get(row, "对方账/卡号", "对方账号", "对方账户"),
        "counterpartyName": _get(row, "对方户名", "对方名称"),
        "groupName": group_name,
        "debitAmount": debit,
        "creditAmount": credit,
        "balance": _to_num(_get(row, "余额")) or 0.0,
        "transactionTime": ttime,
        "transactionSummary": _get(row, "摘要", "用途"),
        "purpose": _get(row, "用途", "摘要"),
        "accountNo": account_no,   # 网银明细账号脱敏，用配置里的完整账号
        "accountName": company,
        "sourceBank": source_bank,
        "ownerCompany": company or owner_company,
        "sourceFile": source_file,
        "currency": _get(row, "币种") or "人民币",
    }
    return _compact(item)


def map_discount(
    row: dict[str, Any],
    *,
    group_name: int,
    source_file: str,
) -> dict[str, Any] | None:
    """长江贴现流水 -> 飞诺 discount-repayment（银行贴现表）。"""
    if not isinstance(row, dict):
        return None
    bill = _get(row, "票据号码", "票据号", "票号")
    if not bill:
        return None
    business = _get(row, "业务日期", "贴现日期", "交易日期", "日期")
    day = business[:10] if business else ""
    ttime = business if (" " in business) else (f"{business} 00:00:00" if business else "")
    item = {
        "billNumber": bill,
        "subBillRange": normalize_bill_range(_get(row, "子票区间", "票据区间")),
        "transactionTime": ttime,
        "businessDate": day,
        "faceAmount": _to_num(_get(row, "票面金额", "票据金额", "金额")) or 0.0,
        "groupName": group_name,
        "sourceFile": source_file,
        # 长江接口若提供这些列则一并映射（缺失则不填，避免污染飞诺）
        "discountInterest": _to_num(_get(row, "贴现利息", "利息")),
        "annualRate": _to_num(_get(row, "年利率", "贴现利率")),
        "drawerName": _get(row, "出票人名称", "出票人"),
        "discountCompany": _get(row, "贴现申请人名称", "贴现申请人"),
        "discountAccount": _get(row, "贴现账号", "贴现账户"),
        "bankName": _get(row, "贴现行名称", "承兑行名称", "银行名称"),
    }
    return _compact(item)


def map_endorse(
    row: dict[str, Any],
    *,
    group_name: int,
    source_file: str,
    owner_company: str = "",
    account_no: str = "",
    source_bank: str = "长江银行",
) -> dict[str, Any] | None:
    """长江背书流水（票据处理查询）-> 飞诺 bank-endorse（银行背书表）。

    长江"票据处理查询"只返回汇总：业务种类/金额/日期/状态 + 处理结果里的业务ID。
    没有票号/背书人/被背书人（需详情接口，当前账号 ENT999090 权限受限）。
    按"汇总字段先推"策略：
      - 票号 billNumber：飞诺表 NOT NULL，暂无真实票号，用业务ID占位（待详情权限打通后换成真票号）
      - 交易流水号 transactionSerial：业务ID（唯一，做幂等键）
      - 背书人/被背书人：留空（飞诺可空）
      - 本方公司/账号/开户行：用配置兜底
    """
    if not isinstance(row, dict):
        return None
    buss_id = _get(row, "业务ID", "业务id", "交易流水号")
    biz_type = _get(row, "业务种类", "业务类型")
    if not buss_id and not biz_type:
        return None

    day = _get(row, "业务日期", "创建日期", "交易日期", "日期")[:10]
    # 金额优先取明细里的纯数字（arg3），其次汇总的"￥x,xxx"
    amount = _to_num(_get(row, "金额")) or _to_num(_get(row, "总金额", "金额"))
    ttime = _get(row, "交易时间") or (f"{day} 00:00:00" if day else "")
    item = {
        "billNumber": _get(row, "票据号码", "票据号", "票号") or buss_id,
        "subBillRange": normalize_bill_range(_get(row, "子票区间", "票据区间")),
        "faceAmount": amount or 0.0,
        "endorser": _get(row, "背书人", "转出方", "背书人名称"),
        "endorsee": _get(row, "被背书人", "转入方", "被背书人名称"),
        "ebankCompany": _get(row, "本方公司") or owner_company,
        "ebankAccount": _get(row, "本方账号", "账号") or account_no,
        "bankName": _get(row, "开户行", "银行名称") or source_bank,
        "businessDate": day,
        "transactionTime": ttime,
        "transactionSerial": _get(row, "交易流水号") or buss_id,
        "busiTypeName": biz_type or _get(row, "业务码"),
        "groupName": group_name,
        "sourceFile": source_file,
    }
    return _compact(item)


# ---------- POST ----------
def _post(path: str, body: dict[str, Any], cfg: dict[str, Any], log: LogFn | None) -> str:
    url = f"{cfg['base_url']}{path}"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Api-Key": cfg["api_key"],
        "Connection": "close",
    }
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                url,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                timeout=cfg["timeout"],
                verify=False,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            payload = json.loads(resp.text) if resp.text else {}
            code = payload.get("code")
            if code not in (200, "200"):
                raise RuntimeError(str(payload.get("msg") or payload)[:300])
            data = payload.get("data") or {}
            msg = (data.get("message") if isinstance(data, dict) else None) or payload.get("msg") or "ok"
            return str(msg)
        except Exception as exc:
            last_exc = exc
            err = str(exc)
            retryable = any(
                x in err for x in (
                    "SSL", "ssl", "TLS", "timed out", "Timeout",
                    "Connection reset", "Connection aborted", "10054", "10060",
                )
            )
            if not retryable or attempt >= 3:
                raise RuntimeError(f"飞诺接口请求失败：{err}") from exc
            _log(log, f"  飞诺请求失败（第 {attempt}/3 次，重试中）：{err[:120]}")
            time.sleep(1.0 * attempt)
    raise RuntimeError(f"飞诺接口请求失败：{last_exc}")


def import_items(path: str, items: list[dict[str, Any]], cfg: dict[str, Any], log: LogFn | None) -> str:
    """分批推送，返回汇总消息；失败按 fail_on_error 决定是否抛出。"""
    if not items:
        return "无可推数据"
    size = cfg["batch_size"]
    msgs: list[str] = []
    failures: list[str] = []
    total = (len(items) + size - 1) // size
    for i in range(0, len(items), size):
        chunk = items[i:i + size]
        batch_no = i // size + 1
        body = {"updateSupport": cfg["update_support"], "items": chunk, "groupName": cfg["group_name"]}
        try:
            msg = _post(path, body, cfg, log)
            msgs.append(msg)
            _log(log, f"  飞诺 {path.rsplit('/', 2)[-2]} 第 {batch_no}/{total} 批：{msg}")
        except Exception as exc:
            failures.append(f"第{batch_no}批：{exc}")
            _log(log, f"  飞诺 {path.rsplit('/', 2)[-2]} 第 {batch_no}/{total} 批失败：{exc}")
            if cfg["fail_on_error"]:
                raise
    if failures:
        msgs.append("失败：" + "; ".join(failures))
    return "; ".join(msgs)


# ---------- 入库后推送入口 ----------
def push_after_save(
    kind: str,
    records: list[dict[str, Any]],
    *,
    account_no: str,
    log: LogFn | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """本地入库成功后调用。返回推送结果说明；未启用/无数据时返回空串。

    kind: 资金流水 / 贴现流水 / 背书流水
    """
    cfg = cfg or api_cfg()
    if not cfg["enabled"]:
        _log(log, "飞诺推送未启用（erp_config.json enabled=false），跳过")
        return ""
    if not cfg["api_key"] or not cfg["base_url"]:
        _log(log, "erp_config.json 缺少 baseUrl / apiKey，跳过飞诺推送")
        return ""
    if not records:
        return ""
    try:
        if kind == "资金流水":
            items = [map_payment(
                r, account_no=account_no, group_name=cfg["group_name"],
                source_bank=cfg["source_bank"], source_file=cfg["source_file"],
                owner_company=cfg["owner_company"],
            ) for r in records]
            items = [x for x in items if x]
            path = "/feinuo-api/payment-flow/import"
            label = "资金流水"
        elif kind == "贴现流水":
            items = [map_discount(r, group_name=cfg["group_name"], source_file=cfg["source_file"]) for r in records]
            items = [x for x in items if x]
            path = "/feinuo-api/discount-repayment/import"
            label = "贴现流水"
        elif kind == "背书流水":
            items = [map_endorse(
                r, group_name=cfg["group_name"], source_file=cfg["source_file"],
                owner_company=cfg["owner_company"], account_no=account_no,
                source_bank=cfg["source_bank"],
            ) for r in records]
            # 按 (票号, 子票区间, 流水号) 去重，避免同一业务重复推送
            seen: set[tuple[str, str, str]] = set()
            deduped: list[dict[str, Any]] = []
            for it in items:
                if not it:
                    continue
                key = (str(it.get("billNumber", "")), str(it.get("subBillRange", "")),
                       str(it.get("transactionSerial", "")))
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(it)
            items = deduped
            path = "/feinuo-api/bank-endorse/import"
            label = "背书流水"
        else:
            return ""
        if not items:
            _log(log, f"{label}：无有效字段可推（{len(records)} 条原始记录被跳过）")
            return ""
        _log(log, f"[飞诺] 推送{label} {len(items)} 条 → 分组 {cfg['group_name']}（{path}）")
        msg = import_items(path, items, cfg, log)
        return f"{label}: {msg}"
    except Exception as exc:
        _log(log, f"[飞诺] {kind} 推送失败：{exc}")
        return f"{kind} 推送失败: {exc}"
