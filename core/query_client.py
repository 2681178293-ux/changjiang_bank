# -*- coding: utf-8 -*-
"""长江网银查询客户端：带登录态 Cookie 发 POST，用标准库解析返回的 HTML 表格。

为什么不用 Playwright/CDP：长江网银在 Edge 里走 IE 模式（MSHTML），
CDP 附加不上、页面 fetch 也执行不了。之前已实测：直接用 requests 带
从 F12 复制的 Cookie POST 到查询接口，返回的就是完整 HTML 表格，解析即可。

Cookie 会过期：配置里的 cookie 失效后，重新登录一次、F12 复制最新 Cookie
贴回 config/query_runtime.json 即可（这一步后续会做自动提取）。
"""
from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import requests
import urllib3

from config.settings import BASE_DIR

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENDPOINTS_PATH = Path(BASE_DIR) / "config" / "query_endpoints.json"
RUNTIME_PATH = Path(BASE_DIR) / "config" / "query_runtime.json"

# 查询重试参数：银行 HTTPS 偶发 SSLV3_ALERT_HANDSHAKE_FAILURE，
# 多为旧 keep-alive 连接被服务端关闭后复用所致——重试并重建连接即可恢复。
POST_MAX_ATTEMPTS = 3
POST_BACKOFF = (0.0, 1.0, 2.0)   # 第 1 次不等，之后递增退避
POST_TIMEOUT = 30
# 这些异常值得重试（SSL/TLS 握手、连接被重置、超时等网络层抖动）
_RETRYABLE_HINTS = (
    "ssl", "tls", "handshake", "sslv3", "alert",
    "timed out", "timeout", "connection reset", "connection aborted",
    "remotedisconnected", "max retries exceeded", "read timed out",
    "10053", "10054", "10060", "10061",
)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", errors="ignore")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class QueryConfig:
    """运行时配置：账号、日期区间、登录态 Cookie。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        d = dict(data or _load_json(RUNTIME_PATH))
        self.account_no: str = str(d.get("account_no") or "").strip()
        self.account_alias: str = str(d.get("account_alias") or "").strip()
        self.begin_date: str = str(d.get("begin_date") or "").strip()
        self.end_date: str = str(d.get("end_date") or "").strip()
        self.cookie: str = str(d.get("cookie") or "").strip()
        self.bank_no: str = str(d.get("bank_no") or "313312300018").strip()
        # 多账号：优先读 account_nos 列表；老配置只有 account_no 时自动兼容成单元素列表
        raw_nos = d.get("account_nos")
        nos = [str(x).strip() for x in raw_nos] if isinstance(raw_nos, list) else []
        self.account_nos: list[str] = [x for x in nos if x]
        if not self.account_nos and self.account_no:
            self.account_nos = [self.account_no]

    @classmethod
    def load(cls) -> "QueryConfig":
        return cls()

    @property
    def accounts(self) -> list[str]:
        """本次要查询的账号列表（去重、保序）。"""
        seen: set[str] = set()
        out: list[str] = []
        for a in self.account_nos:
            if a and a not in seen:
                seen.add(a)
                out.append(a)
        return out

    def with_account(self, account_no: str) -> "QueryConfig":
        """派生一份只换账号的配置（Cookie/日期/银行号不变），供逐账号查询。"""
        return QueryConfig({
            "account_no": account_no,
            "account_nos": [account_no],
            "account_alias": self.account_alias,
            "begin_date": self.begin_date,
            "end_date": self.end_date,
            "cookie": self.cookie,
            "bank_no": self.bank_no,
        })

    def missing(self) -> list[str]:
        miss = []
        if not self.account_no:
            miss.append("account_no")
        if not self.begin_date:
            miss.append("begin_date")
        if not self.end_date:
            miss.append("end_date")
        if not self.cookie:
            miss.append("cookie")
        return miss


class _TableParser(HTMLParser):
    """把 HTML 里所有 <table> 解析成二维文本表格。

    · 单元格里的嵌套 div/span 文本会合并（金额、日期时间分行显示也能收全）；
    · &nbsp;、连续空白折叠成单个空格；
    · 只收 td/th，忽略脚本样式。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []   # table -> rows
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._in_script = True
        elif tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._in_script = False
        elif tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            if self._row is not None:
                self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data: str) -> None:
        if self._in_script or self._cell is None:
            return
        self._cell.append(data)


def parse_tables(html: str) -> list[list[list[str]]]:
    """返回 HTML 中所有表格，每张表是 [行][列] 的文本矩阵。"""
    parser = _TableParser()
    parser.feed(html or "")
    parser.close()
    return parser.tables


def table_to_records(table: list[list[str]]) -> list[dict[str, str]]:
    """把一张表转成记录：第一行当表头，其余行按列对齐。

    自动跳过空行、合计行（首格为空或含"合计/小计/总计"）。
    """
    rows = [r for r in table if any(c.strip() for c in r)]
    if len(rows) < 2:
        return []
    header = [re.sub(r"\s+", "", c) for c in rows[0]]
    # 分页/翻页行只有一两个合并单元格，列数远少于表头，不能当数据
    min_cells = max(2, len(header) // 2)
    records: list[dict[str, str]] = []
    for row in rows[1:]:
        cells = [re.sub(r"\s+", " ", c).strip() for c in row]
        if sum(1 for c in cells if c) < min_cells:
            continue
        first = cells[0] if cells else ""
        if any(k in first for k in ("合计", "小计", "总计", "总笔数")):
            continue
        rec: dict[str, str] = {}
        for i, key in enumerate(header):
            if not key:
                continue
            rec[key] = cells[i] if i < len(cells) else ""
        if any(rec.values()):
            records.append(rec)
    return records


def pick_records(html: str, must_have: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """从响应 HTML 里挑出最像数据表的那张，转成记录。

    策略：优先表头命中 must_have 任一关键词的表；否则取行数最多的表。
    """
    tables = parse_tables(html)
    best: list[dict[str, str]] = []
    for table in tables:
        recs = table_to_records(table)
        if not recs:
            continue
        head = " ".join(table[0]) if table else ""
        hit = any(k in head for k in must_have) if must_have else False
        if hit:
            return recs
        if len(recs) > len(best):
            best = recs
    return best


class _TLSAdapter(requests.adapters.HTTPAdapter):
    """适配银行老网银网关的 TLS 配置。

    实测（2026-11-25，OpenSSL 3.5.7）：网关要求「不安全的旧版重协商」，
    高版本 OpenSSL 默认禁止，直接报 SSLV3_ALERT_HANDSHAKE_FAILURE。
    有效的组合：TLS 1.2 + 默认套件 + SSL_OP_LEGACY_SERVER_CONNECT。
    """

    # ssl.OP_LEGACY_SERVER_CONNECT：允许旧版不安全服务端重协商
    _OP_LEGACY_SERVER_CONNECT = 0x00040000

    def init_poolmanager(self, *args, **kwargs) -> None:
        import ssl
        ctx = ssl.create_default_context()
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        except (AttributeError, ValueError):
            pass
        # 必须显式 set_ciphers：实测仅加 legacy 选项仍 handshake_failure，
        # 加上这行后握手成功（TLS1.2 / AES256-SHA）。
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.options |= self._OP_LEGACY_SERVER_CONNECT
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


class QueryClient:
    """长江网银查询客户端。一个实例复用同一组 Cookie。"""

    def __init__(self, cfg: QueryConfig | None = None, log: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg or QueryConfig.load()
        self.endpoints = _load_json(ENDPOINTS_PATH)
        self.common_headers = dict(self.endpoints.get("headers_common") or {})
        self.log = log
        self.session = requests.Session()
        # 银行老网关要旧版重协商：初始连接就挂 TLS 适配器，
        # 避免首发必然握手失败（GET Pre 页只有一次机会，不能靠重试兜底）。
        self.session.mount("https://", _TLSAdapter())
        self._attempts = 0

    def _say(self, msg: str) -> None:
        if self.log:
            self.log(msg)

    def _new_session(self) -> None:
        """重建连接：丢弃可能已被服务端关闭的 keep-alive 连接。"""
        try:
            self.session.close()
        except Exception:
            pass
        self.session = requests.Session()
        # 今天就拿一个「TLS1.2 适配器」探测；服务端要哪个版本由握手协商，
        # 用限制版本的新连接重试能绕开 TLS 版本不匹配导致的失败。
        self.session.mount("https://", _TLSAdapter())

    def _headers(self, referer: str) -> dict[str, str]:
        h = dict(self.common_headers)
        h["Referer"] = referer
        if self.cfg.cookie:
            h["Cookie"] = self.cfg.cookie
        return h

    def post(self, query_type: str, params: dict[str, Any]) -> str:
        """按 query_endpoints.json 里登记的某一类接口发 POST，返回响应文本。

        网络层错误（SSL 握手失败 / 连接重置 / 超时）自动重试 POST_MAX_ATTEMPTS 次，
        每次新建连接；仍失败才抛出。HTTP 4xx/5xx 不重试（那是明确的业务/服务端错误）。
        """
        q = (self.endpoints.get("query_types") or {}).get(query_type)
        if not q:
            raise ValueError(f"未登记查询类型：{query_type}")
        url = q.get("url")
        referer = q.get("referer") or url
        if not url:
            raise ValueError(f"{query_type} 缺少 url 配置")
        headers = self._headers(referer)
        last_exc: Exception | None = None
        for attempt in range(1, POST_MAX_ATTEMPTS + 1):
            self._attempts = attempt
            try:
                if attempt > 1:
                    self._new_session()
                resp = self.session.post(
                    url,
                    data=params,
                    headers=headers,
                    timeout=POST_TIMEOUT,
                    verify=False,
                )
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
                if attempt > 1:
                    self._say(f"  {query_type}：第 {attempt} 次连接成功")
                return resp.text
            except Exception as exc:
                last_exc = exc
                err = str(exc)
                retryable = any(h in err.lower() for h in _RETRYABLE_HINTS)
                if not retryable or attempt >= POST_MAX_ATTEMPTS:
                    break
                wait = POST_BACKOFF[min(attempt - 1, len(POST_BACKOFF) - 1)]
                self._say(
                    f"  {query_type}：第 {attempt}/{POST_MAX_ATTEMPTS} 次连接失败"
                    f"（{err[:100]}），{wait:g} 秒后重试"
                )
                if wait:
                    time.sleep(wait)
        raise RuntimeError(f"{query_type} 请求失败（已重试 {self._attempts} 次）：{last_exc}") from last_exc
