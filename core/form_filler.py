# -*- coding: utf-8 -*-
"""登录表单自动填写编排 —— 只填登录密码，浏览器已带出客户号/登录名。

流程：
    ① 等页面就绪（客户号、登录名都渲染出来）
    ② 激活 Edge 窗口并置前
    ③ 鼠标点进 ActiveX 密码框（坐标来自 UIA 实时快照）
    ④ 用 Interception 驱动级输入逐字符填密码（普通 keybd_event 会被控件拦截）
    ⑤ 点「登录」按钮
    ⑥ 后续由 service 层等待主界面出现

长江网银登录页实测只有 客户号 / 登录名 / 密码 / 登录按钮 四项，
登录名旁的「防伪」是图片标识而非验证码输入框，因此整条链路可全自动，无需人工。

安全红线：
    · 密码只从凭据读取，绝不明文进日志（只记位数）；
    · 填写期间用 INPUT_LOCK 与 U盾 口令注入互斥，防止焦点互抢；
    · 每字符前校验焦点仍在浏览器窗口，用户切走立即中断；
    · 失败不自动重试密码（避免触发风控/锁定），转人工。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from core import win32_ui as w32
from core.form_locator import FormLocator, FormSnapshot
from core.hid_input import HidInput
from core.logger import Logger, get_logger


@dataclass
class FillResult:
    ok: bool
    stage: str     # ready / activate / click_password / type / click_login
    message: str


class FormFiller:
    """把密码填进长江网银登录页并提交。"""

    def __init__(
        self,
        logger: Logger | None = None,
        locator: FormLocator | None = None,
        hid: HidInput | None = None,
    ) -> None:
        self._log = logger or get_logger("填表")
        self._locator = locator or FormLocator(self._log)
        self._hid = hid

    def fill_password_and_login(
        self,
        edge_hwnd: int,
        password: str,
        *,
        ready_timeout: float = 30.0,
        abort_check=None,
    ) -> FillResult:
        """主入口：等就绪 → 填密码 → 点登录。失败返回结果，不抛异常。"""
        if not password:
            return FillResult(False, "ready", "没有配置登录密码，跳过自动填写")

        # ① 等页面就绪（客户号、登录名都出现）
        self._log.debug("等待登录页客户号/登录名出现…")
        snapshot = self._locator.wait_fields_ready(
            edge_hwnd, timeout=ready_timeout, abort_check=abort_check
        )
        if not snapshot.fields_ready:
            return FillResult(False, "ready", "客户号/登录名未出现（页面未就绪），转人工")
        if not snapshot.actionable:
            return FillResult(False, "ready", "未能定位密码框或登录按钮，转人工")

        # ② 激活浏览器窗口
        if edge_hwnd and not w32.ensure_foreground(edge_hwnd):
            return FillResult(False, "activate", "无法把浏览器窗口切到前台，转人工")

        with w32.INPUT_LOCK:
            w32.FORM_FILLING.set()
            try:
                # 重新取一次快照（激活后坐标可能变化）
                snapshot = self._locator.snapshot(edge_hwnd)
                if not snapshot.actionable:
                    return FillResult(False, "click_password", "密码框坐标已失效，转人工")

                # ③ 点密码框
                x, y = snapshot.password.center
                if not w32.point_reaches_window(edge_hwnd, x, y):
                    return FillResult(False, "click_password", "密码框被遮挡，转人工")
                w32.mouse_click(x, y)
                # ActiveX 密码框首次获得焦点需要稳定期，太早送键会丢头几位。
                time.sleep(0.8)
                if w32.foreground_window() != edge_hwnd:
                    return FillResult(False, "click_password", "点击后焦点不在浏览器，转人工")
                self._log.debug(f"光标已定位到密码框（{x},{y}）")

                # ④ HID 驱动输入密码
                hid = self._hid or HidInput(logger=self._log)
                if not hid.available() and not self._hid:
                    return FillResult(False, "type", f"HID 驱动不可用：{hid.diagnosis}")
                try:
                    # 先用空按键热机，消耗驱动通道一次性丢帧（第一次登录少位数的根因）
                    hid.warmup()
                    time.sleep(0.15)
                    hid.type_text(password, focused_hwnd=edge_hwnd)
                    # 最后一字符送入后控件仍需一点点时间落盘，随后再点登录
                    time.sleep(0.25)
                except Exception as exc:
                    return FillResult(False, "type", f"密码输入中断：{exc}，转人工")
                self._log.debug(f"登录密码已输入（{len(password)}位）")

                # ⑤ 点登录按钮
                fresh = self._locator.snapshot(edge_hwnd)
                button = fresh.login_button or snapshot.login_button
                if not button or not button.valid:
                    return FillResult(False, "click_login", "登录按钮坐标失效，密码已填入请手动点登录")
                bx, by = button.center
                if not w32.point_reaches_window(edge_hwnd, bx, by):
                    return FillResult(False, "click_login", "登录按钮被遮挡，密码已填入请手动点登录")
                w32.mouse_click(bx, by)
                self._log.debug(f"已点击「登录」（{bx},{by}），等待主界面…")
                return FillResult(True, "done", "密码已自动填写并点击登录")
            finally:
                w32.FORM_FILLING.clear()
