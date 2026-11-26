# -*- coding: utf-8 -*-
"""登录表单定位器 —— 用 Windows UI Automation 读取 Edge IE 模式页面。

为什么不用 Playwright / CDP：
    系统策略把 ebank.jindiancloud.com 指定为 IE11 打开，Edge 里这个标签是
    MSHTML（IE 内核），CDP 无法附加。但 IE 内核把页面暴露成 UIA 树，
    实测（2026-11-23）可以直接读到表单控件：
      · 企业客户号 Edit  -> ValuePattern.Value = "02031068402"
      · 登录名 Edit      -> ValuePattern.Value = "xuhui"
      · 登录密码 Edit    -> AutomationId = "102"（ActiveX 安全控件）
      · 登录按钮 Button  -> Name 空、宽度约 294

关键：必须从 IEFrame 子窗口 attach（不是 Edge 顶层窗口），否则读到的是
浏览器 chrome 的按钮/地址栏，看不到页面内容。

定位策略（不写死坐标，窗口移动/缩放自动适应）：
    ① EdgeBrowser.hwnd -> EnumChildWindows 找类名 "IEFrame" 的子窗口；
       找不到时向上/向下兜底扫描所有顶层窗口。
    ② ControlFromHandle(ieframe) 拿到 UIA 根，遍历子树收集 Edit / Button。
    ③ 按结构特征识别字段：
       客户号/登录名 = 有 ValuePattern 的两个普通 Edit（客户号值=纯数字串）；
       密码框 = AutomationId "102" 或位于「登录密码」标签右侧的 Edit；
       登录按钮 = 宽度 > 200 的 ButtonControl。
    ④ 每次取 BoundingRectangle 的实时屏幕坐标，供鼠标点击使用。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass

from core.logger import Logger, get_logger

_EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
_USER32 = ctypes.windll.user32

# 密码框的 AutomationId（长江 ActiveX 控件实测值）；页面改版时可放宽为按位置找
_PASSWORD_AUTOMATION_ID = "102"


@dataclass(frozen=True)
class Element:
    """一个定位到的表单元素及其当前屏幕矩形。"""

    hwnd_source: int      # 来源 IEFrame 句柄
    control_type: str
    automation_id: str
    value: str
    rect: tuple[int, int, int, int]   # (left, top, width, height)

    @property
    def center(self) -> tuple[int, int]:
        left, top, width, height = self.rect
        return (left + width // 2, top + height // 2)

    @property
    def valid(self) -> bool:
        left, top, width, height = self.rect
        return width > 0 and height > 0 and left > -10000 and top > -10000


@dataclass(frozen=True)
class FormSnapshot:
    """一次页面快照：客户号/登录名/密码框/登录按钮的当前状态。"""

    ie_frame: int
    client_no: Element | None
    login_name: Element | None
    password: Element | None
    login_button: Element | None

    @property
    def fields_ready(self) -> bool:
        """客户号与登录名都已渲染出来（用户要求的等待条件）。"""
        return bool(
            self.client_no and self.client_no.valid
            and self.client_no.value.strip()
            and self.login_name and self.login_name.valid
            and self.login_name.value.strip()
        )

    @property
    def actionable(self) -> bool:
        """密码框和登录按钮都定位到了，可以自动填写。"""
        return bool(
            self.password and self.password.valid
            and self.login_button and self.login_button.valid
        )


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _USER32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _find_ie_frames(top_hwnd: int) -> list[int]:
    """找 top_hwnd 下所有 IEFrame 子窗口。"""
    found: list[int] = []

    def cb(child, _lparam):
        if _window_class(child) == "IEFrame":
            found.append(child)
        return True

    try:
        _USER32.EnumChildWindows(top_hwnd, _EnumProc(cb), 0)
    except Exception:
        pass
    return found


def _find_edge_top_windows() -> list[int]:
    """枚举可见的 Edge 顶层窗口（IEFrame 兜底扫描用）。"""
    found: list[int] = []

    def cb(hwnd, _lparam):
        if not _USER32.IsWindowVisible(hwnd):
            return True
        length = _USER32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            _USER32.GetWindowTextW(hwnd, buf, length + 1)
            if "Edge" in buf.value:
                found.append(hwnd)
        return True

    try:
        _USER32.EnumWindows(_EnumProc(cb), 0)
    except Exception:
        pass
    return found


def _to_element(control, ie_frame: int) -> Element:
    try:
        automation_id = str(getattr(control, "AutomationId", "") or "")
    except Exception:
        automation_id = ""
    try:
        value = str(control.GetValuePattern().Value or "")
    except Exception:
        value = ""
    try:
        r = control.BoundingRectangle
        rect = (int(r.left), int(r.top), int(r.width()), int(r.height()))
    except Exception:
        rect = (0, 0, 0, 0)
    return Element(ie_frame, control.ControlTypeName, automation_id, value, rect)


class FormLocator:
    """在 Edge IEFrame 内定位登录表单。"""

    def __init__(self, logger: Logger | None = None) -> None:
        self._log = logger or get_logger("定位")

    # ---------- IEFrame ----------
    def find_ie_frame(self, edge_hwnd: int = 0) -> int:
        """找承载网银页面的 IEFrame 子窗口句柄。"""
        candidates: list[int] = []
        if edge_hwnd:
            candidates.extend(_find_ie_frames(edge_hwnd))
        if not candidates:
            for top in _find_edge_top_windows():
                candidates.extend(_find_ie_frames(top))
        # 优先选标题里带网银域名/银行名的那个
        for frame in candidates:
            try:
                import uiautomation as auto
                root = auto.ControlFromHandle(frame)
                if root and "jindiancloud" in (root.Name or "").lower():
                    return frame
            except Exception:
                pass
        return candidates[0] if candidates else 0

    # ---------- 快照 ----------
    def snapshot(self, edge_hwnd: int = 0) -> FormSnapshot:
        """读取当前页面快照。任何异常都返回空快照，不抛出。"""
        empty = FormSnapshot(0, None, None, None, None)
        frame = self.find_ie_frame(edge_hwnd)
        if not frame:
            return empty
        try:
            import uiautomation as auto
        except Exception as exc:
            self._log.warn(f"uiautomation 不可用：{exc}")
            return empty

        try:
            root = auto.ControlFromHandle(frame)
            if root is None:
                return empty
            edits: list[Element] = []
            buttons: list[Element] = []

            def walk(control, depth: int = 0) -> None:
                if depth > 16:
                    return
                try:
                    ct = control.ControlTypeName
                    if ct == "EditControl":
                        edits.append(_to_element(control, frame))
                    elif ct == "ButtonControl":
                        buttons.append(_to_element(control, frame))
                except Exception:
                    pass
                try:
                    for child in control.GetChildren():
                        walk(child, depth + 1)
                except Exception:
                    pass

            walk(root)

            # 客户号 / 登录名：有值的两个普通 Edit（排除密码框 Aid=102）
            valued = [
                e for e in edits
                if e.value.strip() and e.automation_id != _PASSWORD_AUTOMATION_ID
            ]
            client_no = next(
                (e for e in valued if e.value.strip().isdigit()),
                valued[0] if valued else None,
            )
            login_name = next(
                (e for e in valued if e is not client_no and not e.value.strip().isdigit()),
                None,
            )
            if login_name is None:
                login_name = next((e for e in valued if e is not client_no), None)

            # 密码框：AutomationId 命中；否则取没值的 Edit 中最靠下的一个
            password = next(
                (e for e in edits if e.automation_id == _PASSWORD_AUTOMATION_ID), None
            )
            if password is None:
                valueless = [e for e in edits if not e.value.strip()]
                if valueless:
                    password = max(valueless, key=lambda e: e.rect[1])

            # 登录按钮：宽度最大的 ButtonControl（实测 ~294px）
            login_button = max(buttons, key=lambda e: e.rect[2]) if buttons else None

            return FormSnapshot(frame, client_no, login_name, password, login_button)
        except Exception as exc:
            self._log.warn(f"读取页面快照失败：{type(exc).__name__}: {exc}")
            return empty

    # ---------- 等待 ----------
    def wait_fields_ready(
        self,
        edge_hwnd: int = 0,
        timeout: float = 30.0,
        poll: float = 0.5,
        abort_check=None,
    ) -> FormSnapshot:
        """等待客户号与登录名都出现；返回就绪时的快照。

        超时返回最后一次快照（由调用方决定转人工）。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        last = FormSnapshot(0, None, None, None, None)
        while time.monotonic() < deadline:
            if abort_check and abort_check():
                return last
            last = self.snapshot(edge_hwnd)
            if last.fields_ready:
                self._log.debug(
                    f"页面表单已就绪：客户号={last.client_no.value}, "
                    f"登录名={last.login_name.value}"
                )
                return last
            time.sleep(poll)
        return last
