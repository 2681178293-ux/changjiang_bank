# -*- coding: utf-8 -*-
"""从 iexplore.exe 进程内存读取网银会话 Cookie，自动写回 query_runtime.json。

原理：Edge IE 模式下网银页面由 iexplore.exe 渲染，HTTP 头 Set-Cookie / Cookie
以明文驻留在该进程内存，形如 "GLOBALSESSIONID=xxx; _saberData2=yyy; JSESSIONID=zzz"。
用 OpenProcess + ReadProcessMemory（只读，不注入）扫描这些串，解析出同一会话的
完整 Cookie，使用者无需再开 F12 手动复制。

解析要点（2026-11-25 修复）：
  · 内存里键值对经常「无分号直接粘连」，如
    "GLOBALSESSIONID=T7Ems...BvCookie_ebank=03E67...; _locale=zh_CN"。
    旧正则要求值后必须是 ; / 空白，遇到粘连整段失败，GLOBALSESSIONID 就丢了。
    新做法：把所有已知键的 "key=" 起点当切分点，值截到最近切分点，粘连也能切开。
  · JSESSIONID 与 GLOBALSESSIONID 在内存里未必相邻，不能靠「向后凑一段」分组。
    新做法：JSESSIONID 值形如 0000<token>:-1，其中 token 恰等于 GLOBALSESSIONID，
    用 token 配对，按出现次数选当前活跃会话（历史残留只出现一两次）。
"""
from __future__ import annotations

import ctypes
import json
import re
import ssl
import urllib.request
from collections import Counter
from ctypes import wintypes
from pathlib import Path
from typing import Iterable

from config.settings import BASE_DIR

RUNTIME_PATH = Path(BASE_DIR) / "config" / "query_runtime.json"

# 输出顺序（与 F12 里看到的一致）；这些键同时在内存里用来识别完整 Cookie 串
WANTED_KEYS = (
    "_locale", "_saberData2", "BankId", "Cookie_ebank",
    "GLOBALSESSIONID", "JSESSIONID", "SkinColor", "313312300018",
)

# 除目标键外，把这些 HTTP / Cookie 属性也当切分点，避免 "Path=/..." 之类黏进值
_BOUNDARY_KEYS = (
    "Path", "HttpOnly", "Domain", "Expires", "Max-Age", "Secure",
    "Version", "SameSite", "Comment",
)
_ALL_MARKER_BYTES = [k.encode("ascii") for k in WANTED_KEYS] + \
                    [k.encode("ascii") for k in _BOUNDARY_KEYS]
_WANTED_BYTES = [k.encode("ascii") for k in WANTED_KEYS]

# JSESSIONID: 0000<token>:-1（后面可能跟 /ebank/xxx.do 路径）
_JSESSION_TOKEN_RE = re.compile(r"^\d{4}(.+?):-1")

k32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x20, 0x40, 0x80, 0x01, 0x08}
PROCESS_ACCESS = 0x0010 | 0x0400  # VM_READ | QUERY_INFORMATION


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _iexplore_pids() -> list[int]:
    pids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed))
    out: list[int] = []
    for i in range(needed.value // ctypes.sizeof(wintypes.DWORD)):
        pid = pids[i]
        if not pid:
            continue
        h = k32.OpenProcess(PROCESS_ACCESS, False, pid)
        if not h:
            continue
        buf = ctypes.create_unicode_buffer(260)
        length = wintypes.DWORD(260)
        k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(length))
        name = buf.value.rsplit("\\", 1)[-1].lower()
        k32.CloseHandle(h)
        if name == "iexplore.exe":
            out.append(pid)
    return out


def _read_regions(pid: int, sink) -> None:
    """把进程里所有可读内存块依次交给 sink(data) 处理。"""
    h = k32.OpenProcess(PROCESS_ACCESS, False, pid)
    if not h:
        return
    try:
        addr = 0
        mbi = _MBI()
        CHUNK = 1024 * 1024
        while addr < 0x7FFFFFFFFFFF:
            if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            size = mbi.RegionSize
            if size == 0:
                break
            if mbi.State == MEM_COMMIT and mbi.Protect in READABLE:
                base = mbi.BaseAddress or 0
                off = 0
                while off < size:
                    n = min(CHUNK, size - off)
                    buf = ctypes.create_string_buffer(n)
                    got = ctypes.c_size_t()
                    if k32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got)):
                        sink(buf.raw[:got.value])
                    off += n
            addr = (mbi.BaseAddress or 0) + size
    finally:
        k32.CloseHandle(h)


def _value_end(data: bytes, start: int, cuts: set[int]) -> int:
    """值从 start 起到最近的分隔符 / 不可打印字符 / 下一个已知键起点为止。

    内存里键值对常「无分号直接粘连」（如 GLOBALSESSIONID=xxxCookie_ebank=yyy），
    此时下一个键的 '=' 虽是 cut，但键名本身之前没有分号，值会连键名一起吞进去。
    因此额外检查：一旦遇到任何已知键名（无等号形式）的起点，也在此截断。
    """
    # 已知键名的无等号起点（如 "Cookie_ebank"），用于切掉粘连
    name_starts: set[int] = set()
    for kb in _ALL_MARKER_BYTES:
        s = 0
        while True:
            p = data.find(kb, s)
            if p < 0:
                break
            if p > start:  # 只关心值内部的粘连点
                name_starts.add(p)
            s = p + 1
    i = start
    while i < len(data):
        c = data[i]
        if c == 0x3B or c in (0x00, 0x0D, 0x0A) or not (0x20 <= c <= 0x7E) or i in cuts or i in name_starts:
            break
        i += 1
    return i


def _pick_value(key: str, counts: Counter) -> str:
    """从一个键的多个候选值里挑出最可能是「当前完整值」的一个。"""
    def acceptable(raw: bytes) -> bool:
        s = raw.decode("ascii", "ignore")
        if not s:
            return False
        if key == "BankId":
            return s.isdigit()
        # 明显不是纯 Cookie 值的噪声（URL 参数、HTTP 头、JS 片段、引号等）
        if any(x in s for x in ("&", " ", "HTTP", "getCookies", "(", ")", "\\", "\"", "'")):
            return False
        return True

    cands = [(cnt, raw) for raw, cnt in counts.items() if acceptable(raw)]
    if not cands:
        return ""
    # 丢弃「是某个更长候选的严格前缀」的截断值（如 03E67、b088d、313312）
    strings = {raw.decode("ascii", "ignore") for _, raw in cands}
    non_prefix = [
        (cnt, raw) for cnt, raw in cands
        if not any(
            s != raw.decode("ascii", "ignore") and s.startswith(raw.decode("ascii", "ignore"))
            for s in strings
        )
    ]
    if non_prefix:
        cands = non_prefix
    # 出现次数优先，其次取更长（更完整）的值
    cnt, raw = max(cands, key=lambda t: (t[0], len(t[1])))
    return raw.decode("ascii", "ignore")


_OP_LEGACY_SERVER_CONNECT = 0x00040000
_LOGIN_CHECK_URL = "https://ebank.jindiancloud.com/ebank/ActTrsQryPre.do"


def _session_is_valid(session: dict[str, str]) -> bool:
    """用一个轻量 GET 验证会话是否有效（未返回登录页即为有效）。

    银行网关要旧版重协商，这里用与查询客户端相同的 TLS 配置（纯标准库）。
    """
    try:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.options |= _OP_LEGACY_SERVER_CONNECT
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        headers = {"Cookie": build_cookie_string(session), "User-Agent":
                   "Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko"}
        req = urllib.request.Request(_LOGIN_CHECK_URL, headers=headers)
        with opener.open(req, timeout=8) as resp:
            enc = resp.headers.get_content_charset() or "utf-8"
            html = resp.read(20000).decode(enc, errors="replace")
        # 登录页特征：含 prelogin / 用户名+密码输入框；有效页含账号下拉 AcNo
        if "prelogin" in html.lower():
            return False
        if ("用户名" in html and "密码" in html) and "AcNo" not in html:
            return False
        return True
    except Exception:
        return False


def read_cookie() -> dict[str, str]:
    """读取当前网银会话 Cookie，返回 {键: 值}。

    内存里同一会话的 Cookie 反复出现，历史会话也有残留。这里先收集所有键值，
    再用 JSESSIONID 的 token 与 GLOBALSESSIONID 配对，选出现次数最多的会话。
    """
    counts: dict[str, Counter] = {k: Counter() for k in WANTED_KEYS}

    def sink(data: bytes) -> None:
        # 所有已知键「key=」的起点，作为值的统一切分点（解决无分号粘连）
        cuts: set[int] = set()
        for kb in _ALL_MARKER_BYTES:
            needle = kb + b"="
            s = 0
            while True:
                p = data.find(needle, s)
                if p < 0:
                    break
                cuts.add(p)
                s = p + 1
        for kb in _WANTED_BYTES:
            needle = kb + b"="
            s = 0
            while True:
                p = data.find(needle, s)
                if p < 0:
                    break
                vs = p + len(needle)
                ve = _value_end(data, vs, cuts)
                if ve > vs:
                    val = data[vs:ve]
                    name = kb.decode("ascii")
                    # JSESSIONID 值在内存里常在 :-1 后粘连 URL 路径
                    # （如 0000token:-1/ebank/GetBankName.do），规范化成
                    # 0000token:-1，否则每个变体只出现 1 次、无法按频次选会话。
                    if name == "JSESSIONID":
                        m = re.match(rb"(\d{4}.+?:-1)", val)
                        if m:
                            val = m.group(1)
                    counts[name][val] += 1
                s = vs

    for pid in _iexplore_pids():
        _read_regions(pid, sink)

    if not counts:
        return {}

    # 用 JSESSIONID 里的 token 配对 GLOBALSESSIONID，选当前活跃会话。
    # 注意（2026-11-25 修复）：内存里同时存在多个会话，且同一 token 有粘连变体
    # （如 "cEwN3...Cookie_ebank"）。若把 GLOBALSESSIONID 的次数也累加进来，
    # 粘连变体会把已失效的旧会话顶成最高分。活跃会话以「JSESSIONID 出现次数」
    # 为准（浏览器当前会话的请求头反复出现），GLOBALSESSIONID 仅用于配对确认。
    sessions: Counter = Counter()
    for js_val, js_cnt in counts["JSESSIONID"].items():
        m = _JSESSION_TOKEN_RE.match(js_val.decode("ascii", "ignore"))
        if not m:
            continue
        token = m.group(1)
        for gv, g_cnt in counts["GLOBALSESSIONID"].items():
            g = gv.decode("ascii", "ignore")
            if g and (g == token or g in token or token in g):
                # 只按 JSESSIONID 次数打分；同一 token 取最长（最完整）的 glob
                key = (f"0000{token}:-1", token if len(token) >= len(g) else g)
                sessions[key] += js_cnt

    values: dict[str, str] = {}
    if sessions:
        # 多个会话候选时，光靠出现次数选不可靠（实测两会话打平、且高分那个已失效）。
        # 用轻量 GET 逐个验证，挑出真正有效的会话。
        ranked = [jsid_glob for jsid_glob, _ in sessions.most_common()]
        chosen: tuple[str, str] | None = None
        if len(ranked) > 1:
            for jsid, glob in ranked:
                cand = {k: _pick_value(k, counts[k]) for k in WANTED_KEYS}
                cand["JSESSIONID"] = jsid
                cand["GLOBALSESSIONID"] = glob
                cand = {k: v for k, v in cand.items() if v}
                if _session_is_valid(cand):
                    chosen = (jsid, glob)
                    break
        if chosen is None:
            # 只有一个候选，或都验证失败（网络不可达时）——退回频次最高的
            chosen = ranked[0]
        values["JSESSIONID"], values["GLOBALSESSIONID"] = chosen
    else:
        # 配不出时退回：两键各自取最优候选（refresh_cookie 仍会校验完整性）
        for key in ("JSESSIONID", "GLOBALSESSIONID"):
            values[key] = _pick_value(key, counts[key])

    for key in WANTED_KEYS:
        if key not in values:
            values[key] = _pick_value(key, counts[key])

    return {k: values[k] for k in WANTED_KEYS if values.get(k)}


def load_runtime() -> dict:
    if not RUNTIME_PATH.exists():
        return {}
    raw = RUNTIME_PATH.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        data = json.loads(raw.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def build_cookie_string(values: dict[str, str], keys: Iterable[str] = WANTED_KEYS) -> str:
    """按键顺序拼成 "k=v; k=v" 形式，只保留有值的键。"""
    return "; ".join(f"{k}={values[k]}" for k in keys if values.get(k))


def refresh_cookie() -> tuple[str, dict[str, str]]:
    """读取并写回配置。返回 (完整Cookie串, 找到的键值)。

    只有读到关键会话 Cookie（JSESSIONID + GLOBALSESSIONID）才写盘，
    避免把残留的半截数据覆盖掉已有配置。
    """
    values = read_cookie()
    critical = ("JSESSIONID", "GLOBALSESSIONID")
    if not all(values.get(k) for k in critical):
        return "", values
    cookie_str = build_cookie_string(values)
    data = load_runtime()
    data["cookie"] = cookie_str
    RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return cookie_str, values
