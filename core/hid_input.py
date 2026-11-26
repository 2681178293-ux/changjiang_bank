# -*- coding: utf-8 -*-
"""驱动级键盘输入（Interception）——突破银行 ActiveX 安全控件。

分层定位：
    core/win32_ui.py      用户态输入（keybd_event / SendInput），带 LLKHF_INJECTED
                          标志，会被安全控件拦截。用于 U盾 PIN 等原生 Win32 弹窗。
    core/hid_input.py     本模块，内核过滤驱动级输入，发出不带注入标志的硬件
                          扫描码。专用于网页内的 ActiveX 密码框。

为什么需要这一层：
    2026-11-23 实测：长江网银登录密码框（PowerEnter ActiveX）拦截一切用户态
    合成输入（虚拟键码 / Unicode / 消息投递 / 剪贴板 / 日志回放全部无效），
    但 Interception 驱动级输入可成功写入（用户实测确认）。

关键行为：
    · 本模块**不调用 set_filter**，不会拦截真实键盘，不会造成"键盘失灵"；
    · 逐字符发送，每字符前做焦点 / 存活守卫，焦点被切走立即中断，
      防止密码后半段打进别的窗口（残缺 + 明文泄漏）；
    · 密码绝不明文进日志，只记录位数。

设备选择：
    interception 库的 auto_capture_devices 可能在 ACPI 虚拟设备 / 真实键盘之间
    乱跳（实测会丢首字符）。这里按 HWID 打分，优先选真实物理 USB 键盘（VID_），
    跳过 ACPI 虚拟设备；可用 kb_device 手动固定。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from core import win32_ui as w32

# Interception 支持的字符：字母 / 数字 / 常见符号（走扫描码映射）
_PUNCT = set("!@#$%^&*()-_=+[]{};:',./?\\|`~\"<> ")


class HidUnavailableError(RuntimeError):
    """驱动或 Python 库不可用。"""


class HidFocusLostError(RuntimeError):
    """注入过程中焦点被切走 / 窗口消失，已中断。"""


class HidInput:
    """Interception 驱动级键盘（可复用实例）。

    用法：
        kb = HidInput(interval=0.12)
        if not kb.available():
            ...  # 转人工
        kb.type_text(password, focused_hwnd=browser.hwnd)
    """

    def __init__(
        self,
        interval: float = 0.12,
        kb_device: int | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.interval = max(0.03, float(interval))
        self.kb_device = kb_device
        self._log = logger
        self._ic: Any = None
        self._device = -1
        self.diagnosis = ""

    # ---------- 驱动 ----------
    def available(self) -> bool:
        """检测库与驱动是否就绪，并选定键盘设备。不抛异常。"""
        try:
            import interception as ic  # type: ignore
        except Exception as exc:
            self.diagnosis = (
                f"导入 interception 失败：{exc}。"
                "先 pip install interception-python pywin32，确认驱动已装并重启。"
            )
            return False
        try:
            self._ic = ic
            self._device = self._pick_keyboard(ic, self.kb_device)
            if self._log:
                self._log.debug(f"HID 驱动就绪，键盘设备号 = {self._device}")
            return True
        except Exception as exc:
            self.diagnosis = f"Interception 驱动未就绪：{exc}。确认安装成功并重启。"
            return False

    @staticmethod
    def _pick_keyboard(ic: Any, override: int | None = None) -> int:
        """选一个真实物理键盘设备号，跳过 ACPI 虚拟设备（实测会丢字）。"""
        if override is not None:
            ic.set_devices(keyboard=int(override))
            return int(override)

        ic.auto_capture_devices(keyboard=True, mouse=False)
        try:
            from interception import inputs as _inputs  # type: ignore
            ctx = _inputs._g_context
            best_num, best_score = None, -1
            for n in range(0, 16):
                hwid = ctx.devices[n].get_HWID()
                if not hwid or not ctx.is_keyboard(n):
                    continue
                up = hwid.upper()
                if "ACPI" in up:
                    score = 0            # 虚拟设备，最后才选
                elif "DDHID" in up:
                    score = 1            # DD 虚拟键盘，次选
                elif "VID_" in up:
                    score = 2            # 真实物理 USB 键盘，首选
                else:
                    score = 1
                if score > best_score:
                    best_num, best_score = n, score
            if best_num is not None:
                ic.set_devices(keyboard=best_num)
                return best_num
        except Exception:
            pass
        return ic.get_keyboard()

    # ---------- 输入 ----------
    def warmup(self, strokes: int = 4) -> None:
        """热机：先用无害的修饰键打几下，把首次写入的丢帧消耗掉。

        实测（2026-11-26 用户反馈）：第一次登录密码会少 1~2 位，关窗再登录就正常。
        原因是 Interception 设备/句柄刚切换后，前 1~2 次 send 会被吞；同一进程内
        第二次登录复用已热的通道就正常——典型的一次性初始化丢帧。

        这里在输入真密码前先按几下 Shift（不改变任何输入内容），把丢帧消耗在
        预热上。必须在 available() 之后、密码框已获得焦点时调用。
        """
        if self._ic is None:
            raise HidUnavailableError(self.diagnosis)
        for _ in range(max(1, strokes)):
            try:
                self._ic.press("shiftleft")
            except Exception as exc:
                raise HidUnavailableError(f"HID 热机失败：{exc}") from exc
            time.sleep(self.interval)
        if self._log:
            self._log.debug(f"HID 通道已热机（{strokes} 次空按键）")

    def type_text(self, text: str, focused_hwnd: int = 0) -> None:
        """逐字符发送文本；focused_hwnd 非 0 时每字符前校验焦点与窗口存活。

        焦点丢失 / 窗口消失立即抛 HidFocusLostError，调用方必须转人工，
        绝不继续注入剩余字符。
        """
        if self._ic is None and not self.available():
            raise HidUnavailableError(self.diagnosis)

        for index, ch in enumerate(text, start=1):
            if not self._is_supported(ch):
                raise ValueError(f"HID 不支持的字符：{ch!r}（仅字母/数字/常见符号）")
            if focused_hwnd:
                if not w32.is_alive(focused_hwnd) or not w32.is_visible(focused_hwnd):
                    raise HidFocusLostError(
                        f"输入第 {index}/{len(text)} 位前窗口已关闭/不可见，已中断"
                    )
                if w32.foreground_window() != focused_hwnd:
                    raise HidFocusLostError(
                        f"输入第 {index}/{len(text)} 位前焦点被切走，已中断"
                    )
            try:
                self._ic.write(ch, interval=0)
            except Exception as exc:
                raise HidUnavailableError(f"HID 发送第 {index} 位失败：{exc}") from exc
            time.sleep(self.interval)

    @staticmethod
    def _is_supported(ch: str) -> bool:
        return ch.isascii() and (ch.isalnum() or ch in _PUNCT)


def type_text(text: str, focused_hwnd: int = 0, interval: float = 0.12) -> None:
    """便捷函数：一次性输入（内部建临时实例）。"""
    HidInput(interval=interval).type_text(text, focused_hwnd=focused_hwnd)
