# -*- coding: utf-8 -*-
"""长江网银三类查询：资金流水 / 贴现流水 / 背书流水。

每类做三件事：① 用运行时配置填参数模板 → ② 发 POST → ③ 解析 HTML 表格为记录。
输出统一为 list[dict]：key 用银行表头的中文名（便于对照抓包），
标准化字段名留给入库/飞诺映射那一步处理。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from core.logger import Logger, get_logger
from core.query_client import QueryClient, QueryConfig, pick_records


def normalize_bill_range(value: str) -> str:
    """把子票区间统一成 `A-B`（短横线连接）。

    长江网银隐藏字段 cdRange 返回的是逗号分隔（如 `007615000001,007715000000`），
    而票面展示/飞诺习惯用短横线（如 `007615000001-007715000000`）。
    兼容全角逗号、顿号、分号、空格，以及已经是短横线的情况。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    for ch in ("，", "、", "；", ";"):
        s = s.replace(ch, ",")
    segments: list[str] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        # 已经是 A-B 的也拆开再拼，保证全程只有一个短横线风格
        segments.extend(x.strip() for x in part.split("-") if x.strip())
    return "-".join(segments)


def _detect_total_pages(html: str) -> int:
    """从响应 HTML 里探测总页数；找不到返回 1。"""
    text = html or ""
    patterns = (
        r"共\s*(\d+)\s*页", r"总共\s*(\d+)\s*页", r"总页数\s*[:：]?\s*(\d+)",
        r"totalPage(?:s|Num|Count)?\s*[:=]\s*['\"]?(\d+)",
        r"pageCount\s*[:=]\s*['\"]?(\d+)",
        r"第\s*\d+\s*页\s*/\s*共\s*(\d+)\s*页",
    )
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                n = int(m.group(1))
                if 1 <= n <= 10000:
                    return n
            except (ValueError, IndexError):
                pass
    return 1


def _detect_page_param(html: str) -> str | None:
    """从翻页链接/脚本里探测翻页参数名。"""
    text = html or ""
    for name in ("currentPage", "pageNo", "pageNum", "pageIndex", "curPage", "PageNo", "page"):
        # 匹配 ?pageNo= / 'pageNo': / (pageNo= / pageNo= 等写法
        if re.search(r"(?<![A-Za-z])" + name + r"\s*[:=]", text, re.I):
            return name
    return None

# 每类查询的表头关键词（用于从响应多张表里认出数据表）
HEAD_HINTS: dict[str, tuple[str, ...]] = {
    "资金流水": ("交易日期", "收入支出项", "对方户名"),
    "贴现流水": ("票据号码", "票面金额", "贴现"),
    "背书流水": ("票据号码", "背书", "交易流水号"),
}

LogFn = Callable[[str], None]


def _fmt_date(value: str) -> str:
    """配置支持 2026-11-23 / 2026/11/23 / 20261123，统一成 YYYY-MM-DD。"""
    s = re.sub(r"[\s/]", "-", str(value or "").strip())
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _fill(
    template: dict[str, Any],
    cfg: QueryConfig,
    query_type: str,
    extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把参数模板里的占位符替换成实际值。

    extra 用于补运行时才知道的占位符（如贴现的动态账号字段名 {账号字段}），
    key 与 value 中的占位符都会替换。
    """
    begin = _fmt_date(cfg.begin_date)
    end = _fmt_date(cfg.end_date)
    values: dict[str, str] = {
        "账号": cfg.account_no,
        "账号别名": cfg.account_alias,
        "起始日期": begin,
        "结束日期": end,
        "申请日期": begin,   # 贴现按起始日查；需要区间时由上层追加
        "日期": end,
        "业务类型": "EndorsementProEnsureResult",   # 背书流水默认查背书转让结果
        "金额": "",
        "流水号": "",
        "业务码": "",
        "页码": "1",
        "每页条数": "",
    }
    if extra:
        values.update(extra)

    def sub(text: str) -> str:
        for ph, phval in values.items():
            text = text.replace("{" + ph + "}", str(phval))
        return text

    filled: dict[str, Any] = {}
    for key, val in template.items():
        k = sub(str(key))
        if isinstance(val, str):
            filled[k] = sub(val)
        else:
            filled[k] = val
    return filled


def discount_account_field(client: QueryClient, cfg: QueryConfig, account_no: str) -> str:
    """生成贴现接口的动态账号字段名。

    二代电子票据「贴现结果查询」的账号字段名不是固定的，而是随当前 U 盾的
    账号尾号/账户类型/公司名变化，形如：
        acctNo$2434#一般存款账户#南京卓益安金属材料有限公司
    用登录态 GET 贴现 Pre 页，从 <option value=完整账号>展示文本</option> 里
    拼出这个字段名。取不到时返回空串（该字段值为空，仅 key 重要）。
    """
    q = (client.endpoints.get("query_types") or {}).get("贴现流水") or {}
    page = q.get("pre_page") or "https://ebank.jindiancloud.com/ebank/DiscountResultQryCPESQryPre.do"
    try:
        # 复用客户端已配置好 TLS 适配器的 session，自动带 Cookie
        resp = client.session.get(page, headers=client._headers(page), timeout=20, verify=False)
        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text
    except Exception:
        # 探测失败不致命：该字段值为空、仅 key 重要，缺失时仍能查
        # （探测已确认 cst_account + cust_account 才是账号校验的关键字段）
        return ""
    # 优先取与本次账号一致的 option
    options = re.findall(
        r'<option[^>]*value=["\']?(\d{10,25})["\']?[^>]*>(.*?)</option>',
        html, re.S | re.I,
    )
    chosen = None
    for val, label in options:
        if val == account_no:
            chosen = (val, label)
            break
    if chosen is None and options:
        chosen = options[0]
    if not chosen:
        return ""
    label = re.sub(r"<[^>]+>", "", chosen[1]).strip()
    parts = [p.strip() for p in label.split("/") if p.strip()]
    if len(parts) < 3:
        return ""
    tail = chosen[0][-4:]
    return f"acctNo${tail}#{parts[1]}#{parts[2]}"


def parse_discount_rows(html: str) -> list[dict[str, str]]:
    """解析「贴现结果查询」响应：公开表格列 + 每行隐藏 input 字段。

    公开列只有 票据包号/子票区间/票据种类/票据包金额/业务方向/业务状态，
    业务日期、票面金额、出票人等都在隐藏字段 List{n}xxx 里，必须一起提取，
    否则入库/飞诺映射拿不到「业务日期/票面金额」。

    产出标准键：票据号码/子票区间/票面金额/业务日期/票据种类/业务状态，
    以及贴现利率/贴现利息/出票人名称/收款人名称/承兑人名称等（供飞诺映射兜底）。
    """
    records: list[dict[str, str]] = []
    # 每个数据行 <tr ...> ... </tr>
    for row in re.findall(r"<tr\b.*?</tr>", html or "", re.S | re.I):
        # 收集该行所有 hidden input: id="List{n}<name>" value="..."
        hidden: dict[str, str] = {}
        for m in re.finditer(
            r'id=["\']List\d+(\w+)["\']\s+value=["\']([^"\']*)["\']',
            row, re.I,
        ):
            hidden[m.group(1)] = m.group(2)
        # 没有隐藏字段的行（表头/分页行）跳过
        if not hidden.get("cdNo"):
            continue
        rec: dict[str, str] = {
            "票据号码": hidden.get("cdNo", ""),
            "子票区间": normalize_bill_range(hidden.get("cdRange", "")),
            "票面金额": hidden.get("isseAmt", ""),
            # 业务日期优先用结算日 setDate，其次请求日 reqDt
            "业务日期": hidden.get("setDate") or hidden.get("reqDt") or "",
            "票据种类": {"AC01": "银票", "AC02": "商票"}.get(
                hidden.get("drftTyp", ""), hidden.get("drftTyp", "")
            ),
            "业务状态": hidden.get("txnStat", ""),
            "业务方向": "请求方" if hidden.get("bussFlg") == "01" else hidden.get("bussFlg", ""),
            "贴现利率": hidden.get("dscntRate", ""),
            "贴现利息": hidden.get("intAmt", ""),
            "结算金额": hidden.get("setAmt", ""),
            "出票人名称": hidden.get("drwrNm", ""),
            "收款人名称": hidden.get("pyeeNm", ""),
            "承兑人名称": hidden.get("accptrNm", ""),
            # 贴现申请人（本方）/贴现账户/开户行 -> 飞诺 discountCompany/discountAccount/bankName
            # 银行用 reqNm/fundsAcctNo/rcvNm，映射函数再用别名兜底不同网银版本
            "贴现申请人名称": hidden.get("reqNm") or hidden.get("reqAcctNm", ""),
            "贴现账号": hidden.get("fundsAcctNo") or hidden.get("reqAcct", ""),
            "贴现行名称": hidden.get("rcvNm", ""),
            "出票日": hidden.get("isseDt", ""),
            "到期日": hidden.get("dueDt", ""),
            "业务ID": hidden.get("bussId", ""),
        }
        records.append(rec)
    return records


# showBillDetailDiv('589845345','许慧','1','3730000.00','2026-11-18','DiscountApplyCPES','0','0')
# 参数含义（实测 2026-11-25）：业务ID / 操作人 / 标志 / 金额 / 日期 / 业务码 / 未知 / 状态序号
_ENDORSE_DETAIL_RE = re.compile(r"showBillDetailDiv\(([^)]*)\)")
_ENDORSE_BUSI_TYPE = {
    "DiscountApplyCPES": "贴现申请",
    "BillBusCPESAcceptResult": "提示保证承兑",
    "AutoDiscountApplyResult": "自动贴现申请",
    "EndorsementProEnsureResult": "背书提示保证",
    "EndorsementAttornApplyResult": "背书转让申请",
    "BatchEndorsementProEnsure": "背书批量提示保证",
    "BatchEndorsementApplyRes": "批量背书转让申请",
}


def parse_endorse_rows(html: str) -> list[dict[str, str]]:
    """解析「票据处理查询」响应：公开汇总列 + 处理结果 onclick 里的业务明细。

    公开列只有 业务种类/总金额/创建日期/状态/操作，票号等明细在"处理结果"链接的
    showBillDetailDiv(...) 参数里（业务ID、金额、日期、业务码）。这里把两者合成记录，
    供入库和飞诺 bank-endorse 映射使用。

    产出键：业务种类/总金额/创建日期/状态/业务ID/金额/业务码/操作人/交易流水号。
    """
    records: list[dict[str, str]] = []
    for row in re.findall(r"<tr\b.*?</tr>", html or "", re.S | re.I):
        # 处理结果 onclick 参数（数据行必有 showBillDetailDiv）
        m = _ENDORSE_DETAIL_RE.search(row)
        if not m:
            # 无详情链接：表头/分页/提示行，跳过（分页行曾被误收成一条记录）
            continue
        args = [a.strip().strip("\"'") for a in m.group(1).split(",")]
        # 公开列单元格
        cells = [
            " ".join(re.sub(r"<[^>]+>", " ", c).split())
            for c in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S | re.I)
        ]

        def arg(i: int) -> str:
            return args[i] if i < len(args) else ""

        biz_code = arg(5)
        biz_type = cells[0] if cells else ""
        buss_id = arg(0)
        amount = arg(3) or (cells[1] if len(cells) > 1 else "")
        day = arg(4) or (cells[2] if len(cells) > 2 else "")

        # 跳过表头/空行/分页行：至少要有业务种类或业务ID
        if not biz_type and not buss_id:
            continue
        if biz_type in ("业务种类", "标题") or "没有查询到" in " ".join(cells):
            continue
        rec: dict[str, str] = {
            "业务种类": biz_type or _ENDORSE_BUSI_TYPE.get(biz_code, ""),
            "总金额": cells[1] if len(cells) > 1 else "",
            "创建日期": cells[2] if len(cells) > 2 else "",
            "状态": cells[3] if len(cells) > 3 else "",
            "业务ID": buss_id,
            "金额": amount,
            "业务日期": day,
            "业务码": biz_code,
            "操作人": arg(1),
            # 交易流水号用业务ID兜底：飞诺 bank-endorse 唯一键需要它，
            # 长江汇总无独立流水号，业务ID每条唯一，可作幂等键。
            "交易流水号": arg(6) and f"{buss_id}-{arg(6)}" or buss_id,
        }
        records.append(rec)
    return records


class ChangjiangQuery:
    def __init__(self, cfg: QueryConfig | None = None, logger: Logger | None = None) -> None:
        self.cfg = cfg or QueryConfig.load()
        self.log = logger or get_logger("查询")
        # 把日志回调传给客户端，SSL 重试信息才能在界面显示
        self.client = QueryClient(self.cfg, log=self.log.info)

    def available(self) -> list[str]:
        return list((self.client.endpoints.get("query_types") or {}).keys())

    def query(
        self, query_type: str, param_overrides: dict[str, Any] | None = None
    ) -> list[dict[str, str]]:
        """查一类数据，返回记录列表。param_overrides 用于翻页时覆盖个别参数。"""
        miss = self.cfg.missing()
        if miss:
            raise ValueError(f"查询配置未填完整：{', '.join(miss)}（config/query_runtime.json）")
        template = self._template(query_type)
        extra: dict[str, str] = {}
        # 贴现接口的账号字段名随当前盾动态变化，需先探测 Pre 页生成
        if query_type == "贴现流水":
            field = discount_account_field(self.client, self.cfg, self.cfg.account_no)
            extra["账号字段"] = field
            self.log.debug(f"贴现账号字段名：{field or '(空值字段)'}")
        params = _fill(template, self.cfg, query_type, extra=extra)
        if param_overrides:
            params.update(param_overrides)
        hints = HEAD_HINTS.get(query_type, ())
        all_records: list[dict[str, str]] = []
        try:
            html = self.client.post(query_type, params)
            if query_type == "贴现流水":
                # 二代电子票据返回的隐藏字段含业务日期/金额，用专用解析器
                recs = parse_discount_rows(html)
                if not recs:
                    err = re.search(r"<PEAjaxError>(.*?)</PEAjaxError>", html, re.S)
                    if err:
                        raise RuntimeError(f"银行返回错误：{err.group(1).strip()}")
            else:
                recs = pick_records(html, hints)
            all_records.extend(recs)
            self.log.debug(f"{query_type}：解析到 {len(recs)} 条")
        except Exception as exc:
            self.log.warn(f"{query_type} 查询失败：{exc}")
            raise
        return all_records

    def query_discount_paged(self) -> list[dict[str, str]]:
        """贴现流水专用：抓全部页（区间一次查，但结果可能分页）。

        分页字段：currentIndex(0起)、pageNumber、recordNumber、counter。
        先查首页 -> 从响应读总页数 -> 逐页覆盖 currentIndex 重发，按票据包号+子票区间去重。
        """
        miss = self.cfg.missing()
        if miss:
            raise ValueError(f"查询配置未填完整：{', '.join(miss)}（config/query_runtime.json）")
        template = self._template("贴现流水")
        field = discount_account_field(self.client, self.cfg, self.cfg.account_no)
        params = _fill(template, self.cfg, "贴现流水", extra={"账号字段": field})

        all_records: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def collect(html: str) -> int:
            err = re.search(r"<PEAjaxError>(.*?)</PEAjaxError>", html, re.S)
            if err:
                raise RuntimeError(f"银行返回错误：{err.group(1).strip()}")
            added = 0
            for r in parse_discount_rows(html):
                key = (r.get("票据号码", ""), r.get("子票区间", ""))
                if key in seen:
                    continue
                seen.add(key)
                all_records.append(r)
                added += 1
            return added

        html = self.client.post("贴现流水", params)
        added = collect(html)
        self.log.debug(f"贴现流水 第1页：解析 {added} 条新记录")
        # 总页数：优先 [共 N 页]，其次 pageNumber 之外没有明显总数时用 recordNumber/counter 估算
        m = re.search(r"共\s*(\d+)\s*页", html)
        total = int(m.group(1)) if m else 1
        if total <= 1:
            return all_records
        self.log.debug(f"贴现流水：检测到共 {total} 页")
        for page in range(2, total + 1):
            try:
                p = dict(params)
                p["currentIndex"] = str((page - 1) * 20)   # 常见每页 20 条；服务端会按实际校正
                p["pageNumber"] = str(page)
                h2 = self.client.post("贴现流水", p)
                added = collect(h2)
                self.log.debug(f"贴现流水 第{page}/{total}页：新增 {added} 条")
            except Exception as exc:
                self.log.warn(f"贴现流水 第{page}页失败，停止翻页：{exc}")
                break
        return all_records

    def query_endorse_paged(self, max_pages: int = 100) -> list[dict[str, str]]:
        """背书流水专用：抓全部页。

        长江接口参数模板里没有翻页字段，翻页参数藏在响应 HTML 的脚本/链接里。
        策略（稳健优先）：
          1. 先查第一页，从响应里解析总页数（共N页 / totalPage / pageCount 等）；
          2. 探测翻页参数名（currentPage/pageNo/pageNum/pageIndex 等，取响应里出现的）；
          3. 逐页 POST，每页记录按内容指纹去重；
          4. 任一页失败/无新数据即停，绝不影响已抓到的页。
        """
        miss = self.cfg.missing()
        if miss:
            raise ValueError(f"查询配置未填完整：{', '.join(miss)}（config/query_runtime.json）")
        template = self._template("背书流水")
        params = _fill(template, self.cfg, "背书流水")
        hints = HEAD_HINTS.get("背书流水", ())

        all_records: list[dict[str, str]] = []
        seen: set[str] = set()

        def collect(html: str) -> int:
            """解析一页 HTML，加入去重后的记录，返回本页新增条数。"""
            # 背书用专用解析器：从"处理结果"onclick 里取业务ID/金额/日期/业务码，
            # 通用 pick_records 只能拿到汇总列，缺飞诺要的唯一键。
            recs = parse_endorse_rows(html) or pick_records(html, hints)
            added = 0
            for r in recs:
                fp = json.dumps(r, ensure_ascii=False, sort_keys=True)
                if fp in seen:
                    continue
                seen.add(fp)
                all_records.append(r)
                added += 1
            return added

        # 第一页
        try:
            html = self.client.post("背书流水", params)
        except Exception as exc:
            self.log.warn(f"背书流水查询失败：{exc}")
            raise
        added = collect(html)
        self.log.debug(f"背书流水 第1页：解析 {added} 条新记录")

        # 解析总页数
        total = _detect_total_pages(html)
        if total <= 1:
            self.log.debug("背书流水：仅 1 页，抓取完成")
            return all_records
        self.log.debug(f"背书流水：检测到共 {total} 页")

        # 探测翻页参数名
        page_param = _detect_page_param(html) or "pageNo"
        self.log.debug(f"背书流水：翻页参数用 {page_param}")

        # 从第 2 页开始逐页抓
        for page in range(2, min(total, max_pages) + 1):
            try:
                p = dict(params)
                p[page_param] = str(page)
                html = self.client.post("背书流水", p)
                added = collect(html)
                self.log.debug(f"背书流水 第{page}/{total}页：新增 {added} 条")
                if added == 0:
                    # 翻页参数可能无效（每页都是第一页），继续尝试但接近尾声可提前停
                    continue
            except Exception as exc:
                self.log.warn(f"背书流水 第{page}页失败，停止翻页：{exc}")
                break
        return all_records

    def query_all(self) -> dict[str, list[dict[str, str]]]:
        """依次查三类，单类失败不影响其他类。"""
        result: dict[str, list[dict[str, str]]] = {}
        for name in ("资金流水", "贴现流水", "背书流水"):
            try:
                result[name] = self.query(name)
            except Exception as exc:
                self.log.warn(f"{name} 跳过：{exc}")
                result[name] = []
        return result

    def _template(self, query_type: str) -> dict[str, Any]:
        q = (self.client.endpoints.get("query_types") or {}).get(query_type)
        if not q:
            raise ValueError(f"未登记查询类型：{query_type}")
        params = q.get("params")
        if not isinstance(params, dict):
            raise ValueError(f"{query_type} 缺少 params 模板")
        return params
