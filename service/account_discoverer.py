# -*- coding: utf-8 -*-
"""自动发现当前 U盾 可访问的银行账号与本方公司名（免手动配置）。

原理：登录成功后，用新鲜 Cookie 请求资金流水查询准备页
ActTrsQryPre.do，页面里 <select name="AcNo"> 的 option：
  value   = 完整账号（如 80200240000100892434）
  option文本 = 脱敏账号/账户类型/本方公司名
              （如 8020****2434/一般存款账户/南京卓益安金属材料有限公司）
换公司U盾登录后，这里就是那家公司的账号和公司名。

解析用标准库正则，不依赖 bs4。
"""
from __future__ import annotations

import re

from core.logger import Logger, get_logger
from core.query_client import QueryClient, QueryConfig

BASE = "https://ebank.jindiancloud.com/ebank/"
PRE_PAGES = (
    BASE + "ActTrsQryPre.do",               # 资金流水查询准备页（AcNo 下拉，option文本带公司名）
    BASE + "BillBusinessHandleQryPre.do",   # 背书/票据业务查询准备页（AcNoNew 账号下拉）
)
ACCOUNT_SELECT_NAMES = ("AcNoNew", "AcNo", "holdAcctNo")

# 账号 option 的 value：纯数字，长度 10-25
_ACCOUNT_RE = re.compile(r"^\d{10,25}$")
# <select name="xxx"> ... </select>
_SELECT_RE = re.compile(
    r"<select\b[^>]*\bname=[\"']?([^\"'\s>]+)[\"']?[^>]*>(.*?)</select>",
    re.S | re.I,
)
# 单个 <option value="账号">展示文本</option>，文本用于提取公司名
_OPTION_RE = re.compile(
    r"<option\b[^>]*\bvalue=[\"']?([^\"'\s>]*)[\"']?[^>]*>(.*?)</option>",
    re.S | re.I,
)
# 账户类型段（要跳过，不能当公司名）
_ACCOUNT_TYPE_WORDS = ("一般存款账户", "基本存款账户", "专用存款账户", "临时存款账户")


def _company_from_label(label: str) -> str:
    """从 option 展示文本里提取本方公司名。

    典型格式：8020****2434/一般存款账户/南京卓益安金属材料有限公司
    按 "/" 切段，跳过账号/账户类型段，取最后一段有效中文名。
    """
    text = re.sub(r"<[^>]+>", "", label or "")
    parts = [p.strip() for p in text.split("/") if p.strip()]
    # 从后往前找：排除账户类型、纯数字、含星号脱敏、过短的段
    for part in reversed(parts):
        if any(w in part for w in _ACCOUNT_TYPE_WORDS):
            continue
        if "*" in part or part.isdigit() or len(part) < 4:
            continue
        return part
    return ""


def parse_account_entries(html: str) -> list[tuple[str, str]]:
    """解析 (完整账号, 本方公司名) 列表，按出现顺序去重。公司名抓不到则为空串。"""
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _SELECT_RE.finditer(html or ""):
        if m.group(1) not in ACCOUNT_SELECT_NAMES:
            continue
        for om in _OPTION_RE.finditer(m.group(2)):
            val = om.group(1).strip()
            if not _ACCOUNT_RE.match(val) or val in seen:
                continue
            seen.add(val)
            entries.append((val, _company_from_label(om.group(2))))
    return entries


def parse_accounts(html: str) -> list[str]:
    """从准备页 HTML 解析账号列表，按出现顺序去重（保留给旧调用）。"""
    return [acc for acc, _ in parse_account_entries(html)]


class AccountDiscoverer:
    def __init__(self, cfg: QueryConfig | None = None, logger: Logger | None = None) -> None:
        self.cfg = cfg or QueryConfig.load()
        self.log = logger or get_logger("账号")

    def discover(self) -> list[str]:
        """依次尝试几个准备页，返回找到的账号列表（保留给旧调用）。"""
        return [acc for acc, _ in self.discover_with_company()]

    def discover_with_company(self) -> tuple[list[str], str]:
        """发现账号 + 本方公司名，返回 (账号列表, 公司名)。

        公司名取第一个账号下拉里解析到的值；同一 U盾 通常属同一家公司。
        """
        client = QueryClient(self.cfg)
        for page in PRE_PAGES:
            try:
                resp = client.session.get(
                    page, headers=client._headers(page), timeout=20, verify=False
                )
                resp.encoding = resp.apparent_encoding or "utf-8"
                html = resp.text
                # 登录态失效会返回登录页，里面没有账号 select，直接跳过
                entries = parse_account_entries(html)
                if entries:
                    accounts = [acc for acc, _ in entries]
                    company = next((c for _, c in entries if c), "")
                    page_name = page.rsplit('/', 1)[-1]
                    self.log.debug(f"从 {page_name} 发现账号 {len(accounts)} 个：{', '.join(accounts)}")
                    if company:
                        self.log.debug(f"从 {page_name} 识别本方公司：{company}")
                    return accounts, company
            except Exception as exc:
                self.log.warn(f"读取账号准备页失败（{page.rsplit('/', 1)[-1]}）：{exc}")
        return [], ""
