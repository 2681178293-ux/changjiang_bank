# -*- coding: utf-8 -*-
"""键盘守卫 —— 自动输入 U盾 PIN 的那几秒里，吞掉用户真实按键、放行脚本注入。

背景：
    注入 PIN 期间如果用户正在打字（比如刚想输验证码），按键可能打进 PIN 框，
    造成残缺/错误口令甚至误提交。用 WH_KEYBOARD_LL 低级键盘钩子区分两者：
      · 事件带 LLKHF_INJECTED(0x10)  → 脚本注入，放行（不影响自动输入）
      · 用户真实按键（无该标志）      → return 1 吞掉，字符打不到任何窗口

实现要点（农行工具真机验证过，照搬）：
    · 低级钩子必须跑在带消息循环的线程里，这里用 PeekMessage 轮询 + Event 退出；
    · **64 位必须显式声明 CallNextHookEx / SetWindowsHookExW 的 argtypes**，
      否则句柄/指针参数按 32 位 c_int 转换溢出，注入键会被误吞（表现为"自动输入没反应"）；
    · start() 同步等钩子真正开始拉消息才返回，避免注入事件漏过回调；
    · 安装失败不阻塞注入：吞键只是增强保护，失败时降级继续（焦点守卫仍生效）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

from core.logger import Logger

user32 = ctypes.windll.user32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
LLKHF_INJECTED = 0x10
PM_REMOVE = 0x0001


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wt.WPARAM, wt.LPARAM)

user32.CallNextHookEx.restype = wt.LPARAM
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.UnhookWindowsHookEx.restype = wt.BOOL

_blocked = 0     # 被吞掉的用户真实按键数
_passed = 0      # 放行的注入事件数
_samples: list[str] = []


def _hook_cb(nCode, wParam, lParam):
    global _blocked, _passed
    if nCode < 0:
        return user32.CallNextHookEx(None, nCode, wParam, lParam)
    kbd = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
    injected = bool(kbd.flags & LLKHF_INJECTED)
    _samples.append(
        f"{'D' if wParam == WM_KEYDOWN else 'U'}:{kbd.vkCode}" + ("*" if injected else "")
    )
    if len(_samples) > 24:
        _samples.pop(0)
    if injected:
        _passed += 1
        return user32.CallNextHookEx(None, nCode, wParam, lParam)
    _blocked += 1
    return 1


_hook_cb_ref = HOOKPROC(_hook_cb)   # 保持引用，防止被 GC 回收导致钩子失效


class KeyboardGuard:
    """注入窗口期的键盘守卫；with 语句用法：`with KeyboardGuard(log): ...`"""

    def __init__(self, log: Logger | None = None) -> None:
        self._log = log or Logger("键盘守卫")
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._hook = None

    def start(self, timeout: float = 1.0) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="keyboard-guard")
        self._thread.start()
        return self._ready.wait(timeout)

    def _run(self) -> None:
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_cb_ref, None, 0)
        if not self._hook:
            self._log.warn(
                f"键盘守卫安装失败（错误码 {ctypes.get_last_error()}），本次注入无吞键保护"
            )
            self._ready.set()
            return
        self._log.debug("键盘守卫已启用：注入期间用户按键会被吞掉")
        msg = wt.MSG()
        try:
            pump_started = False
            while not self._stop.wait(0.02):
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                if not pump_started:
                    pump_started = True
                    self._ready.set()
        finally:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def stop(self) -> None:
        global _blocked, _passed
        self._stop.set()
        if self._thread:
            try:
                self._thread.join(timeout=2)
            except Exception:
                pass
            self._thread = None
        if _blocked or _passed:
            self._log.debug(f"键盘守卫统计：拦截用户按键 {_blocked} 次、放行注入 {_passed} 次")
        _blocked = 0
        _passed = 0
        _samples.clear()

    def __enter__(self) -> "KeyboardGuard":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()
