# -*- coding: utf-8 -*-
"""用本机 Microsoft Edge 打开企业网银，并监控窗口状态（不驱动页面）。

为什么不用 Playwright 驱动 Edge（2026-11 本机实测，两个硬约束）：
    ① 本机 Edge 有策略 `UserDataDir`，会忽略命令行 `--user-data-dir`，
       新进程一律转发给已开实例（Playwright 报 exitCode=21 / 0），拿不到窗口；
    ② 关键：系统策略 `C:\\Windows\\ie_site_list.xml` 已把
       `ebank.jindiancloud.com/ebank/prelogin.do` 指定为 IE11 打开，
       Edge 里这个标签页是 MSHTML（IE 内核），CDP 无法附加，
       `page.url` / `inner_text` 都读不到。

所以这里改成"打开 + 窗口监控"：
    · 用 msedge.exe 打开登录页（用户已开 Edge 时自然复用同一个窗口/新标签）；
    · 通过 Win32 读窗口标题判断：网银页面还在不在、是否已经登录成功。

顺带的好处：IE 内核里密码框是银行 ActiveX 安全控件，可以正常输入密码。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from config.settings import SETTINGS, Settings
from core.browser_types import (
    KIND_BROWSER_CLOSED,
    KIND_CANCELLED,
    KIND_TIMEOUT,
    KIND_UKEY_REMOVED,
    BrowserError,
    LoginProbe,
)
from core.logger import Logger, get_logger

_USER32 = ctypes.windll.user32
_KERNEL32 = ctypes.windll.kernel32
_ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

_EDGE_WINDOW_CLASS = "Chrome_WidgetWin_1"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Edge 在标题里可能夹零宽字符：`Microsoft​ Edge`
_ZWSP = "\u200b"
# 「A 和另外 2 个页面 - 个人 - Microsoft​ Edge」
_PAGE_COUNT_RE = re.compile(r"\s*和另外\s*\d+\s*个页面\s*$")

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


# ---------- Win32 小工具 ----------
def _window_title(hwnd: int) -> str:
    length = _USER32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _USER32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _USER32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_exe(pid: int) -> str:
    handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _KERNEL32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _KERNEL32.CloseHandle(handle)


def _top_windows() -> list[int]:
    out: list[int] = []

    def cb(hwnd, _lparam):
        out.append(hwnd)
        return True

    _USER32.EnumWindows(_ENUMPROC(cb), 0)
    return out


def page_title(full_title: str) -> str:
    """去掉 Edge 的窗口后缀，只留标签页标题。

    `账号查询 - 个人 - Microsoft​ Edge`      -> `账号查询`
    `xxx 和另外 2 个页面 - 个人 - Microsoft​ Edge` -> `xxx`
    """
    title = (full_title or "").replace(_ZWSP, "").strip()
    parts = title.rsplit(" - ", 2)
    if len(parts) == 3 and "edge" in parts[2].lower():
        title = parts[0]
    elif len(parts) >= 2 and "edge" in parts[-1].lower():
        title = " - ".join(parts[:-1])
    return _PAGE_COUNT_RE.sub("", title).strip()


# IE 模式标签的宿主窗口标题：`{url} - {页面标题} - Internet Explorer`
_IE_SUFFIX_RE = re.compile(r"\s*-\s*Internet Explorer\s*$", re.I)


def parse_ie_title(raw: str) -> tuple[str, str]:
    """把 IEFrame 的标题拆成 (当前 URL, 页面标题)；拿不到 URL 时返回空串。"""
    title = _IE_SUFFIX_RE.sub("", (raw or "").replace(_ZWSP, "").strip())
    if title.lower().startswith(("http://", "https://")):
        head, _sep, tail = title.partition(" - ")
        return head.strip(), tail.strip()
    return "", title


def ie_frame_state(hwnd: int) -> tuple[str, str]:
    """读 IE 模式标签当前的 (URL, 页面标题)；不是 IE 模式则返回空串。

    IE 内核把地址放在 IEFrame 的窗口标题里，这是 Playwright/CDP 看不到的信息，
    正好用来判断"还停在登录页"还是"已经进主界面"。
    """
    best: tuple[int, str, str] = (-1, "", "")

    def cb(child, _lparam):
        nonlocal best
        if _window_class(child) != "IEFrame":
            return True
        url, title = parse_ie_title(_window_title(child))
        if not url and not title:
            return True
        rank = 1 if _USER32.IsWindowVisible(child) else 0
        if rank > best[0]:
            best = (rank, url, title)
        return True

    _USER32.EnumChildWindows(hwnd, _ENUMPROC(cb), 0)
    return best[1], best[2]


def find_edge() -> Path | None:
    """找 msedge.exe：先看常见安装路径，再看注册表 App Paths。"""
    for path in _EDGE_CANDIDATES:
        if Path(path).is_file():
            return Path(path)
    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    root,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                ) as key:
                    value = winreg.QueryValue(key, None)
                    if value and Path(value).is_file():
                        return Path(value)
            except OSError:
                continue
    except Exception:  # pragma: no cover - 非 Windows
        pass
    return None


class EdgeBrowser:
    """用 Edge 打开网银，并靠窗口标题判断状态（接口与 BankBrowser 保持一致）。"""

    def __init__(self, settings: Settings = SETTINGS, logger: Logger | None = None) -> None:
        self._s = settings
        self._log = logger or get_logger("Edge")
        self._cancel = threading.Event()
        self._edge: Path | None = None
        self._hwnd = 0
        self._login_title = ""      # 打开时的登录页标题，用来判断"标题变了"
        self.last_url = ""

    # ---------- 对外属性 ----------
    @property
    def is_running(self) -> bool:
        return self._edge is not None and self._hwnd != 0 and self.is_alive()

    @property
    def hwnd(self) -> int:
        """浏览器窗口句柄（自动填表 / 弹窗定位要用它作为起点）。"""
        if not self._hwnd or not _USER32.IsWindow(self._hwnd):
            found = self._find_bank_window()
            if found:
                self._hwnd = found
        return self._hwnd

    @property
    def page_count(self) -> int:
        return 1 if self.is_alive() else 0

    # ---------- 窗口查找 ----------
    def _is_bank_title(self, title: str) -> bool:
        if not title:
            return False
        low = title.lower()
        if any(host.lower() in low for host in self._s.ebank_hosts):
            return True
        return bool(self._s.bank_name and self._s.bank_name in title)

    def _find_bank_window(self) -> int:
        for hwnd in _top_windows():
            if _window_class(hwnd) != _EDGE_WINDOW_CLASS:
                continue
            try:
                if not _USER32.IsWindowVisible(hwnd):
                    continue
            except Exception:
                continue
            if not _process_exe(_window_pid(hwnd)).lower().endswith("msedge.exe"):
                continue
            if self._is_bank_title(_window_title(hwnd)):
                return hwnd
        return 0

    def _current_page_title(self) -> str:
        tag, _url, ie_title = self._read_state()
        return ie_title or tag

    def _read_state(self) -> tuple[str, str, str]:
        """返回 (Edge 标签标题, IE 页面 URL, IE 页面标题)。"""
        if not self._hwnd or not _USER32.IsWindow(self._hwnd):
            found = self._find_bank_window()
            if not found:
                return "", "", ""
            self._hwnd = found
        tag = page_title(_window_title(self._hwnd))
        url, ie_title = ie_frame_state(self._hwnd)
        return tag, url, ie_title

    # ---------- 启动 ----------
    def start(self) -> None:
        self._cancel.clear()
        if self._edge is None:
            self._edge = find_edge()
            if self._edge is None:
                raise BrowserError(
                    "没有找到 Microsoft Edge（msedge.exe）。"
                    "请确认已安装 Edge，或把 config/.env 的 BROWSER_CHANNEL 改回 chrome"
                )
            self._log.debug(f"使用 Microsoft Edge 打开网银：{self._edge}")

        existing = self._find_bank_window()
        if existing:
            self._hwnd = existing
            self._log.debug("Edge 里已经打开网银页面，直接复用这个窗口")

    def _wait_bank_window(self, timeout: float) -> int:
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            if self._cancel.is_set():
                return 0
            found = self._find_bank_window()
            if found:
                return found
            time.sleep(0.4)
        return 0

    # ---------- 打开网银 ----------
    def enter_corp_ebank(self) -> str:
        existing = self._find_bank_window()
        if existing:
            self._hwnd = existing
            if not self._login_title:
                self._login_title = self._current_page_title()
            self.last_url = self._s.ebank_url
            return f"Edge 里已停留在企业网银登录页：{self._s.ebank_url}"

        if self._edge is None:
            self._edge = find_edge()
        if self._edge is None:
            raise BrowserError("没有找到 Microsoft Edge（msedge.exe）")

        url = self._s.ebank_url if self._s.open_ebank_directly else self._s.home_url
        try:
            subprocess.Popen([str(self._edge), url], close_fds=True)
        except Exception as exc:
            raise BrowserError(f"调用 Edge 打开网银失败：{exc}") from exc

        hwnd = self._wait_bank_window(timeout=45)
        if not hwnd:
            raise BrowserError(
                "已调用 Edge 打开网银，但没有等到网银窗口出现。"
                "请确认 Edge 能正常上网后重新点「登录」"
            )
        self._hwnd = hwnd
        self._login_title = self._current_page_title()
        self.last_url = self._s.ebank_url
        return f"已用 Edge 打开企业网银登录页：{self._s.ebank_url}"

    def click_ebank_login(self) -> str | None:
        """长江的登录页自带表单，点了等于提交空表单，所以不代点。"""
        return None

    def login_form_ready(self) -> bool:
        return bool(self._current_page_title())

    # ---------- 状态判断 ----------
    def is_alive(self) -> bool:
        """Edge 窗口还在就算浏览器还开着（用户切到别的标签也算）。"""
        if self._hwnd and _USER32.IsWindow(self._hwnd):
            if _process_exe(_window_pid(self._hwnd)).lower().endswith("msedge.exe"):
                return True
        found = self._find_bank_window()
        if found:
            self._hwnd = found
            return True
        return False

    def _main_menu_hit(self) -> str:
        """只读探测网银页面是否出现主界面专属菜单，命中返回文案，否则返回空串。

        长江银行 2026-11 实测：登录成功后地址栏仍是 `prelogin.do`，
        标签标题也保持「企业网上银行|江苏长江商业银行」不变——靠 URL/标题
        都识别不出来。但 MSHTML 页面能通过 Windows UI Automation 遍历到菜单
        文字（我的主页 / 查询服务 / 待审核交易 …），登录页不会有这些词。

        这里是**只读**探测：只查名字、不点击、不输入，不涉及任何表单自动化。
        失败（未装 uiautomation / 窗口已关 / 页面还没加载）一律返回空串，
        由调用方回退到标题判断，不能因为探测失败就卡住登录。
        """
        if not self._hwnd or not _USER32.IsWindow(self._hwnd):
            return ""
        try:
            import uiautomation as auto
        except Exception:
            return ""
        try:
            root = auto.ControlFromHandle(self._hwnd)
            if root is None:
                return ""
            for text in self._s.success_page_texts:
                try:
                    # 用正则包含匹配：控件名可能带换行/空格/图标前缀，精确匹配会漏掉
                    if root.Control(searchDepth=30, NameRe=f".*{text}.*").Exists(0.3):
                        return text
                except Exception:
                    continue
        except Exception:
            return ""
        return ""

    def _looks_logged_in(
        self, tag_title: str, ie_url: str = "", ie_title: str = ""
    ) -> bool:
        # ① 最可靠：页面出现主界面专属菜单（只读 UIA 探测，URL/标题不变也能识别）
        if self._main_menu_hit():
            return True

        page = ie_title or tag_title
        if not page and not ie_url:
            return False

        # URL 比较一律先去掉查询串：`LoginType=C` 会把 "login" 带进来
        probe_url = (ie_url or "").split("?")[0].lower()
        probe_text = (page or "").split("?")[0].lower()

        # ② 页面标题出现成功文案（安全退出 / 账户查询 …）
        if any(text in page for text in self._s.success_texts):
            return True

        # ③ 当前 URL 已经进到主界面路径
        if ie_url and any(k in probe_url for k in self._s.success_url_keywords):
            return True

        # ④ 标题离开了登录页、仍是本行页面。
        #    注意：不能再排除「企业网上银行」——长江登录成功后标题正是
        #    「企业网上银行|江苏长江商业银行」，旧代码在这里被误判为未登录。
        if (
            self._login_title
            and page
            and page != self._login_title
            and self._is_bank_title(page)
            and "登录" not in page
        ):
            return True
        return False

    # ---------- 等待登录 ----------
    def wait_login(
        self,
        timeout: int | None = None,
        poll: float | None = None,
        abort_check: "Callable[[], str] | None" = None,
    ) -> LoginProbe:
        """轮询窗口标题，直到登录成功 / 浏览器被关 / U盾 被拔 / 超时。"""
        timeout = self._s.login_wait_seconds if timeout is None else timeout
        poll = self._s.login_poll_seconds if poll is None else poll
        deadline = time.time() + max(0, timeout)
        hits = 0
        told_ready = False

        while time.time() < deadline:
            if self._cancel.is_set():
                return LoginProbe(False, "已取消", KIND_CANCELLED)

            # ① 用户把 Edge 关了：立刻结束，别傻等超时
            if not self.is_alive():
                return LoginProbe(False, "Edge 已关闭", KIND_BROWSER_CLOSED)

            # ② 外部条件不满足（U盾 被拔）：交给上层决定怎么提示
            if abort_check is not None:
                try:
                    reason = abort_check()
                except Exception:
                    reason = ""
                if reason:
                    return LoginProbe(False, reason, KIND_UKEY_REMOVED)

            tag, ie_url, ie_title = self._read_state()
            page = ie_title or tag
            if page and not told_ready:
                told_ready = True
                self._log.debug(f"网银页面已就绪（{page}），等待你输入登录信息…")

            if self._looks_logged_in(tag, ie_url, ie_title):
                hits += 1
                if hits >= max(1, self._s.success_confirm_times):
                    menu = self._main_menu_hit()
                    reason = f"网银主界面已出现「{menu}」" if menu else f"已进入网银主界面：{page}"
                    return LoginProbe(True, reason)
            else:
                hits = 0

            time.sleep(poll)

        return LoginProbe(False, f"等待超时（{timeout} 秒）", KIND_TIMEOUT)

    # ---------- 收尾 ----------
    def cancel(self) -> None:
        self._cancel.set()

    def close(self) -> None:
        """Edge 是用户自己的浏览器，程序退出时**不关它**，只断开监控。"""
        self._hwnd = 0
        self.last_url = ""
