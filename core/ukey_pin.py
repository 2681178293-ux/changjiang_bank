# -*- coding: utf-8 -*-
"""U盾 PIN 弹窗 / 证书选择弹窗 的自动处理（后台监控线程）。

要解决的两件事：
    ① U盾 口令弹窗 —— 银行安全控件 / CSP 弹出的原生窗口，标题各家不同，
       靠「标题白名单 + 自适应识别」找到它，抢焦点后逐字符注入 PIN 并回车；
    ② 证书选择弹窗 —— Windows 的「选择证书」对话框，自动选中第一张证书并确认
       （等价于农行工具里的做法，解决"每次都要手点一下证书"）。

为什么不直接找 Edit 控件塞值：
    很多 U盾 口令框是自绘的，密码框不是标准 Edit（甚至没有 Edit 子控件），
    WM_SETTEXT 无效。所以统一走"真人路径"：抢到前台 → 真实按键 → 回车，
    与人工操作完全一致，CSP 也无法区分。

安全红线（比功能更重要，务必保留）：
    · 单次注入失败**绝不自动重试**：口令连错会锁 U盾，只能去网点补办；
    · PIN 不落日志、不打印明文（日志只显示"已输入 N 位"）；
    · 连续失败达到阈值熔断，本次运行转人工；
    · 注入期间只认"焦点在自己身上"的窗口，用户切走立即中断，
      避免 PIN 后半段打进别的窗口（残缺 + 泄漏）；
    · 注入那几秒用 KeyboardGuard 吞掉真人按键，防止用户同时打字混入。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import ctypes
import ctypes.wintypes as wt

from config.settings import SETTINGS, Settings
from core import win32_ui as w32
from core.keyboard_guard import KeyboardGuard
from core.logger import Logger

BM_CLICK = 0x00F5
LVM_FIRST = 0x1000
LVM_SETITEMSTATE = LVM_FIRST + 43
LVIS_SELECTED = 0x0002
LVIS_FOCUSED = 0x0001

CERT_POPUP_TEXT = "选择证书"


# ------------------------------------------------------------
# 证书选择弹窗：选中第一行 + 点确定
# ------------------------------------------------------------
def _select_first_listview_item(hwnd: int) -> None:
    lv = w32.find_child_by_class(hwnd, "SysListView32")
    if not lv:
        return

    class LVITEM(ctypes.Structure):
        _fields_ = [
            ("mask", ctypes.c_uint), ("iItem", ctypes.c_int),
            ("iSubItem", ctypes.c_int), ("state", ctypes.c_uint),
            ("stateMask", ctypes.c_uint), ("pszText", wt.LPWSTR),
            ("cchTextMax", ctypes.c_int), ("iImage", ctypes.c_int),
            ("lParam", wt.LPARAM), ("iIndent", ctypes.c_int),
            ("iGroupId", ctypes.c_int), ("cColumns", ctypes.c_uint),
            ("puColumns", ctypes.POINTER(ctypes.c_uint)),
            ("piColFmt", ctypes.POINTER(ctypes.c_int)),
            ("iGroup", ctypes.c_int),
        ]

    item = LVITEM()
    item.stateMask = LVIS_SELECTED | LVIS_FOCUSED
    item.state = LVIS_SELECTED | LVIS_FOCUSED
    w32.user32.SendMessageW(lv, LVM_SETITEMSTATE, 0, ctypes.byref(item))


def _click_ok_by_position(hwnd: int) -> None:
    """自绘窗口（无标准按钮）兜底：按右下角估算「确定」按钮位置并点击。"""
    left, top, right, bottom = w32.window_rect(hwnd)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return
    w32.mouse_click(right - int(width * 0.15), bottom - int(height * 0.08))


def confirm_cert_popup(hwnd: int, log: Logger) -> bool:
    """自动确认「选择证书」：优先选中第一行再点标准「确定」按钮。"""
    with w32.INPUT_LOCK:
        w32.activate_window(hwnd)
        if w32.foreground_window() != hwnd:
            w32.force_foreground_by_click(hwnd)

        button = w32.find_child_button(hwnd, "确定") or w32.find_child_button(hwnd, "OK")
        if button:
            _select_first_listview_item(hwnd)
            w32.user32.SendMessageW(button, BM_CLICK, 0, 0)
            log.debug("已自动选中第一张证书并点「确定」")
            return True

        log.debug("证书框没有标准「确定」按钮（自绘），改用坐标 + 回车兜底")
        _select_first_listview_item(hwnd)
        _click_ok_by_position(hwnd)
        w32.press_enter()
        return True


# ------------------------------------------------------------
# PIN 弹窗识别
# ------------------------------------------------------------
@dataclass
class PopupStats:
    """一次登录过程中的弹窗处理情况（日志汇总用）。"""

    cert_confirmed: int = 0
    pin_found: int = 0
    pin_submitted: int = 0
    pin_failed: int = 0
    unknown: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.cert_confirmed:
            parts.append(f"证书弹窗自动确认 {self.cert_confirmed} 次")
        if self.pin_found:
            parts.append(f"U盾 口令弹窗 {self.pin_found} 次")
        if self.pin_submitted:
            parts.append(f"自动提交 {self.pin_submitted} 次")
        if self.pin_failed:
            parts.append(f"失败 {self.pin_failed} 次")
        return "、".join(parts) if parts else "期间没有出现弹窗"


def _looks_like_pin_title(title: str, keywords: tuple[str, ...]) -> bool:
    return any(k in title for k in keywords)


def _count_edits(hwnd: int) -> int:
    return len(w32.find_children_by_class(hwnd, "edit"))


def find_pin_popup(
    baseline: set[int],
    attempts: set[int],
    settings: Settings,
    log: Logger,
    allow_unknown: bool = True,
) -> int:
    """找一个"看起来是 U盾 口令框"的窗口；找不到返回 0。

    两级策略：
      ① 白名单标题（settings.ukey_pin_window_titles）—— 命中即用，最可靠；
      ② 自适应识别 —— 登录期间**新出现**的窗口，且同时满足：
           有 Edit 子控件 / 有「确定」类按钮 / 尺寸像对话框 / 标题含密码口令类词，
         打分足够高才认。识别到的标题会写进日志，用户可据此改成白名单。
    """
    hwnd = w32.find_window_by_title(settings.ukey_pin_window_titles)
    if hwnd and hwnd not in attempts and CERT_POPUP_TEXT not in w32.window_title(hwnd):
        return hwnd

    if not allow_unknown:
        return 0

    best_score = 0
    best_hwnd = 0
    for candidate in w32.top_windows(visible_only=True):
        if candidate in baseline or candidate in attempts:
            continue
        info = w32.describe_window(candidate)
        if not info.title.strip():
            continue
        if CERT_POPUP_TEXT in info.title:        # 证书框交给 confirm_cert_popup
            continue
        if info.width > 900 or info.height > 700 or info.width < 120:
            continue
        edits = _count_edits(candidate)
        if edits == 0:
            continue
        has_ok = bool(
            w32.find_child_button(candidate, "确定")
            or w32.find_child_button(candidate, "确认")
            or w32.find_child_button(candidate, "OK")
        )
        score = 0
        if info.cls == "#32770":
            score += 2
        if has_ok:
            score += 2
        if edits == 1:
            score += 1
        if _looks_like_pin_title(info.title, settings.pin_title_keywords):
            score += 3
        if score > best_score:
            best_score, best_hwnd = score, candidate

    # 阈值 5：至少"有确定按钮 + 有输入框 + 标题含密码/口令"这个组合才敢动键盘
    if best_hwnd and best_score >= 5:
        info = w32.describe_window(best_hwnd)
        if not _looks_like_pin_title(info.title, settings.pin_title_keywords):
            log.warn(f"发现可疑弹窗（{info.describe()}），标题不含密码类词，暂不自动输入")
            return 0
        log.warn(
            f"识别到未登记的口令弹窗：{info.describe()}；"
            f"如确认是 U盾 口令框，可把标题加进 config/.env 的 UKEY_PIN_WINDOW_TITLES"
        )
        return best_hwnd
    return 0


# ------------------------------------------------------------
# PIN 注入
# ------------------------------------------------------------
def _edit_length(hwnd: int) -> int:
    """读第一个 Edit 的字符数；没有 Edit 返回 -1（自绘口令框常见）。"""
    edits = w32.find_children_by_class(hwnd, "edit")
    if not edits:
        return -1
    try:
        return w32.user32.GetWindowTextLengthW(edits[0]) or 0
    except Exception:
        return -1


def inject_pin(hwnd: int, pin: str, log: Logger, settings: Settings) -> tuple[bool, str]:
    """向口令弹窗注入 PIN 并回车。返回 (是否成功, 说明)。

    成功判据只有一条：**提交后弹窗消失**（口令错时弹窗不关）。
    失败一律不重试，交给上层转人工。
    """
    if not pin:
        return False, "未配置 U盾 口令（PIN 为空），请先在「设置 · 登录」里填写"

    guard = KeyboardGuard(log)
    try:
        with w32.INPUT_LOCK:
            w32.activate_window(hwnd)
            if w32.foreground_window() != hwnd:
                w32.force_foreground_by_click(hwnd)
            if w32.foreground_window() != hwnd:
                return False, "口令弹窗抢不到前台焦点（可能被其他窗口挡住）"

            # 钩子从安装到真正生效有几百毫秒延迟，等一下就绪后再注入
            guard.start()
            time.sleep(0.4)

            log.debug(f"正在自动输入 U盾 口令（{len(pin)} 位）…")
            w32.type_text(pin, hwnd, interval=settings.pin_inject_interval)

            length = _edit_length(hwnd)
            if length > 0 and length != len(pin):
                # 有 Edit 且位数不对 → 注入残缺，绝不能提交（会白白消耗错次）
                return False, f"口令框只有 {length} 位（应为 {len(pin)}），已拒绝提交"
            if length <= 0:
                log.debug("口令框读不到字符数（安全控件屏蔽或自绘），按已输入处理")

            if w32.foreground_window() != hwnd:
                return False, "回车前焦点被切走，已中断（转人工输入）"
            time.sleep(0.3)
            w32.press_enter()
    except Exception as exc:
        return False, f"自动输入异常：{exc}"
    finally:
        guard.stop()

    if w32.wait_window_closed(hwnd, settings.pin_commit_seconds):
        return True, "口令已提交，弹窗已关闭"
    return False, "提交后弹窗仍未关闭（口令错误或被拦截），请人工处理"


# ------------------------------------------------------------
# 后台监控线程
# ------------------------------------------------------------
class PopupWatcher(threading.Thread):
    """登录期间盯着弹窗：证书框自动确认、U盾 口令框自动输入。

    用法：
        watcher = PopupWatcher(log, pin_getter=lambda: "12345678")
        watcher.start()
        ... 等登录 ...
        watcher.stop()          # 内部会打印汇总
    """

    def __init__(
        self,
        log: Logger,
        pin_getter: Callable[[], str],
        settings: Settings = SETTINGS,
        *,
        auto_pin: bool = True,
        auto_confirm_cert: bool = True,
        allow_unknown_title: bool = True,
        poll: float = 0.35,
    ) -> None:
        super().__init__(daemon=True, name="popup-watcher")
        self._log = log
        self._pin_getter = pin_getter
        self._s = settings
        self._auto_pin = auto_pin
        self._auto_cert = auto_confirm_cert
        self._allow_unknown = allow_unknown_title
        self._poll = poll
        self._stop = threading.Event()
        self._baseline: set[int] = set()
        self._pin_attempts: set[int] = set()
        self._cert_attempts: set[int] = set()
        self._fail_count = 0
        self._last_try: dict[int, float] = {}
        self.stats = PopupStats()

    # ---------- 生命周期 ----------
    def prime(self) -> None:
        """记录"当前已存在的窗口"，之后只认新出现的（避免误伤用户已开的窗口）。"""
        self._baseline = w32.snapshot_windows()

    def stop(self) -> None:
        self._stop.set()
        if self.is_alive():
            self.join(timeout=3)

    def relog(self, log: Logger) -> None:
        self._log = log

    # ---------- 主循环 ----------
    def run(self) -> None:
        if not self._baseline:
            self.prime()
        while not self._stop.wait(self._poll):
            try:
                if self._auto_cert:
                    self._watch_cert()
                if self._auto_pin:
                    self._watch_pin()
            except Exception as exc:
                self._log.debug(f"弹窗监控异常：{exc}")

    def _watch_cert(self) -> None:
        if w32.FORM_FILLING.is_set():      # 表单正在填，别抢焦点
            return
        hwnd = w32.find_window_prefix(CERT_POPUP_TEXT)
        if not hwnd:
            hwnd = w32.find_window_contains(CERT_POPUP_TEXT)
        if not hwnd or hwnd in self._cert_attempts:
            return
        now = time.time()
        if now - self._last_try.get(hwnd, 0) < 1.0:
            return
        self._last_try[hwnd] = now
        self._cert_attempts.add(hwnd)
        self._log.debug("检测到「选择证书」弹窗，自动选择第一张证书…")
        try:
            confirm_cert_popup(hwnd, self._log)
            self.stats.cert_confirmed += 1
        except Exception as exc:
            self._log.warn(f"自动确认证书失败：{exc}（请在浏览器里手动选一下）")

    def _watch_pin(self) -> None:
        if self._fail_count >= self._s.pin_max_fail_per_run:
            return
        if w32.FORM_FILLING.is_set():      # 表单正在填，等它填完再输口令
            return
        hwnd = find_pin_popup(
            self._baseline, self._pin_attempts, self._s, self._log, self._allow_unknown
        )
        if not hwnd:
            return
        self._pin_attempts.add(hwnd)
        self.stats.pin_found += 1
        ok, reason = inject_pin(hwnd, self._pin_getter(), self._log, self._s)
        if ok:
            self.stats.pin_submitted += 1
            self._fail_count = 0
            self._log.info(f"✔ {reason}")
        else:
            self.stats.pin_failed += 1
            self._fail_count += 1
            self._log.warn(f"✘ {reason}（本次不自动重试，避免锁 U盾）")

    def mark_cert_done(self) -> None:
        """外部（预热/一次性场景）已处理证书框，避免重复点。"""
        hwnd = w32.find_window_prefix(CERT_POPUP_TEXT)
        if hwnd:
            self._cert_attempts.add(hwnd)


# ------------------------------------------------------------
# 诊断：把当前所有窗口列一遍（不知道弹窗标题时用）
# ------------------------------------------------------------
def diagnose(log: Logger) -> None:
    log.info("—— 当前可见顶层窗口（找弹窗标题用）——")
    for hwnd in w32.top_windows(visible_only=True):
        info = w32.describe_window(hwnd)
        if not info.title.strip():
            continue
        edits = _count_edits(hwnd)
        mark = "  ← 有输入框" if edits else ""
        log.info(f"  {info.describe()}{mark}")
