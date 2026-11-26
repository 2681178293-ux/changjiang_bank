# -*- coding: utf-8 -*-
"""同步业务编排：按勾选类型抓数 -> 增量入库 -> 视类型推送飞诺。

分层位置：
    core/query_client.py   发 POST + 解析 HTML（纯网络/解析）
    core/query_cjccb.py    单类查询封装
    core/db.py             增量入库（按唯一键去重）
    service/feinuo_api.py  飞诺字段映射 + 推送（资金/贴现/背书三类）
    service/sync_service.py【本文件】编排"查哪些、入库、按类型推送、汇总结果"
    ui/sync_worker.py      后台线程，把本服务搬到非 UI 线程执行
    main.py                只收集界面参数 + 显示结果

本服务不依赖 Qt，可单独测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from core import db
from service import feinuo_api
from core.logger import Logger, get_logger
from core.query_client import QueryConfig
from core.query_cjccb import ChangjiangQuery

# 本次要做的三类（票面类接口权限受限，不纳入）
# 三类均抓数+入库+推送飞诺（背书流水 2026-11-25 起按新要求接入推送）
SUPPORTED_KINDS = ("资金流水", "贴现流水", "背书流水")
NO_PUSH_KINDS = ()


@dataclass
class SyncOutcome:
    """单类同步结果，供 UI 显示。"""
    kind: str
    fetched: int = 0     # 抓到的记录数（所有账号合计）
    written: int = 0     # 实际入库行数（新增+覆盖，所有账号合计）
    ok: bool = True
    message: str = ""
    accounts: int = 0    # 本次查了几个账号


def _date_range(begin: str, end: str) -> list[str]:
    """把起止日期展开成逐日列表 YYYY-MM-DD；解析失败时返回单日。"""
    def parse(s: str) -> date | None:
        s = str(s or "").strip().replace("/", "-").replace(":", "-")
        try:
            y, m, d = (int(x) for x in s.split("-")[:3])
            return date(y, m, d)
        except (ValueError, TypeError):
            return None

    b, e = parse(begin), parse(end)
    if b is None or e is None:
        return [str(begin or "").strip()]
    if e < b:
        b, e = e, b
    days: list[str] = []
    cur = b
    while cur <= e:
        days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days or [b.strftime("%Y-%m-%d")]


class SyncService:
    """抓数 + 入库 + 按类型推送的编排服务，不碰 UI。"""

    def __init__(self, cfg: QueryConfig | None = None, logger: Logger | None = None) -> None:
        self.cfg = cfg or QueryConfig.load()
        self.log = logger or get_logger("同步")

    def _cfg_for_range(self, begin: str, end: str, account: str = "") -> QueryConfig:
        """复用配置里的账号/Cookie，但日期用界面传入的区间；account 指定本次查询账号。"""
        base = self.cfg
        cfg = base.with_account(account) if account else QueryConfig({
            "account_no": base.account_no,
            "account_alias": base.account_alias,
            "begin_date": begin,
            "end_date": end,
            "cookie": base.cookie,
            "bank_no": base.bank_no,
        })
        cfg.begin_date = begin
        cfg.end_date = end
        return cfg

    def check_ready(self, kinds: Iterable[str], begin: str = "", end: str = "") -> list[str]:
        """同步前校验，返回阻塞原因（空列表=可以开始）。

        账号/Cookie 来自配置，日期来自界面——按各自真实来源检查，
        不能直接用 cfg.missing()（它会把配置里空着的日期也算成阻塞）。
        """
        reasons: list[str] = []
        picked = [k for k in kinds if k in SUPPORTED_KINDS]
        if not picked:
            reasons.append("未勾选「资金流水」「贴现流水」或「背书流水」")
        if not str(begin or "").strip() or not str(end or "").strip():
            reasons.append("请先在界面选好起始日期和结束日期")
        if not self.cfg.accounts:
            reasons.append("账号 account_no / account_nos 未填（config/query_runtime.json）")
        if not self.cfg.cookie:
            reasons.append("登录态 Cookie 未填（config/query_runtime.json，登录后 F12 复制）")
        return reasons

    def sync_one(self, kind: str, begin: str, end: str, account: str = "") -> SyncOutcome:
        """同步一类、一个账号：抓数 -> 入库。"""
        query = ChangjiangQuery(self._cfg_for_range(begin, end, account), logger=self.log.child(kind))
        records: list[dict[str, str]] = []

        if kind == "背书流水":
            # 背书接口带分页，抓全部页
            records = query.query_endorse_paged()
        elif kind == "贴现流水":
            # 贴现按业务日期区间查（一次请求），但结果可能分页，抓全部页
            records = query.query_discount_paged()
        else:
            # 资金流水(BeginDate/EndDate)支持区间，一次查完
            records = query.query(kind)

        written = db.upsert(kind, records, account_no=account)
        self.log.info(f"{kind} [{self._tail(account)}]：抓取 {len(records)} 条，入库 {written} 条")

        # 入库成功后推送飞诺（背书流水按要求不推送）
        if records and kind not in NO_PUSH_KINDS:
            push_msg = feinuo_api.push_after_save(
                kind, records, account_no=account, log=self.log.info
            )
            if push_msg:
                self.log.info(f"{kind} [{self._tail(account)}] 推送结果：{push_msg}")
        elif kind in NO_PUSH_KINDS:
            self.log.info(f"{kind}：按设置不推送飞诺，仅入本地库")

        return SyncOutcome(kind=kind, fetched=len(records), written=written, accounts=1)

    @staticmethod
    def _tail(account: str) -> str:
        """日志里账号只显示后4位，避免整号刷屏。"""
        return "…" + account[-4:] if account else "(无账号)"

    def sync(self, kinds: Iterable[str], begin: str, end: str) -> list[SyncOutcome]:
        """按勾选类型 × 每个账号同步；单账号失败不影响其它账号/类型，结果合并成每类一条。"""
        # 同步前强制从浏览器内存刷新一次会话 Cookie 并重载配置：
        # 避免"登录后立刻点同步"时读到上一次的旧 Cookie（会话已失效→抓 0 条）。
        # 刷新失败不影响同步（沿用配置里已有的 Cookie，由查询层报错）。
        try:
            from core.cookie_reader import refresh_cookie
            cookie, values = refresh_cookie()
            if cookie:
                self.log.debug(f"[同步] 已刷新登录态 Cookie（{len(values)} 项）")
                self.cfg = QueryConfig.load()
            else:
                self.log.warn("[同步] Cookie 刷新未取到新会话，沿用配置现有 Cookie")
        except Exception as exc:
            self.log.warn(f"[同步] Cookie 刷新跳过（不影响本次同步）：{exc}")

        accounts = self.cfg.accounts or [""]
        results: list[SyncOutcome] = []
        for kind in SUPPORTED_KINDS:
            if kind not in kinds:
                continue
            self.log.debug(f"{kind}：开始同步 {len(accounts)} 个账号")
            merged = SyncOutcome(kind=kind, accounts=len(accounts))
            for account in accounts:
                try:
                    one = self.sync_one(kind, begin, end, account)
                    merged.fetched += one.fetched
                    merged.written += one.written
                except Exception as exc:
                    merged.ok = False
                    merged.message += f"账号 {self._tail(account)} 失败：{exc}; "
                    self.log.warn(f"{kind} [{self._tail(account)}] 同步失败：{exc}")
            self.log.info(
                f"{kind} 合计：{len(accounts)} 个账号，抓取 {merged.fetched} 条，入库 {merged.written} 条"
            )
            results.append(merged)
        return results
