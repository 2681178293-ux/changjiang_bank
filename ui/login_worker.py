# -*- coding: utf-8 -*-
"""把登录流程放到后台线程，避免阻塞 Qt 界面；日志通过信号回到 UI 线程。

线程生命周期（重要）：
    ukey → 打开浏览器 → 等待用户输密码 → 判定成功
        └─ 成功后**不退出线程**，继续在本线程里监控会话（浏览器被关 / U盾 被拔）。
        浏览器关掉、或程序要退出（shutdown）时才结束线程。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from config.settings import SETTINGS, Settings
from core.logger import Logger
from service.changjiang_service import LoginResult, ChangjiangBankService

# 这些情况下浏览器已经没了/不该再提示"重新点击登录"，由界面给出对应文案
_QUIET_STAGES = ("cancelled", "browser_closed", "ukey_removed")


class LoginWorker(QThread):
    """一次登录任务的执行者（登录成功后继续充当会话监控者）。"""

    log_line = Signal(str)          # 一整行日志文本
    stage_changed = Signal(str)     # ukey / browser / click / wait
    session_event = Signal(str, str)   # 登录成功后的会话事件：(kind, 提示文案)
    finished_with = Signal(bool, str, str)  # (是否成功, stage, 说明)

    def __init__(self, settings: Settings = SETTINGS, parent=None) -> None:
        super().__init__(parent)
        self._s = settings
        self._service: ChangjiangBankService | None = None
        self.result: LoginResult | None = None
        self.monitoring = False     # True = 登录已成功，正在盯会话

    @property
    def service(self) -> ChangjiangBankService | None:
        return self._service

    def run(self) -> None:  # noqa: D401 - QThread 入口
        logger = Logger("登录", sinks=[self.log_line.emit])
        self._service = ChangjiangBankService(self._s, logger)
        try:
            result = self._service.login(on_stage=self.stage_changed.emit)
        except Exception as exc:  # 兜底：任何异常都要回报给界面
            logger.error(f"登录流程异常：{exc}")
            result = LoginResult(False, "error", str(exc))
        self.result = result

        if not result.ok:
            self._service.close()
            if result.stage not in _QUIET_STAGES:
                logger.warn("问题处理后请重新点击「登录」")
            self.finished_with.emit(False, result.stage, result.message)
            return

        self.finished_with.emit(True, result.stage, result.message)

        # 登录成功后继续盯会话，直到浏览器被关掉 / 被要求退出
        self.monitoring = True
        try:
            self._service.watch_session(self.session_event.emit)
        except Exception as exc:
            logger.warn(f"会话监控结束：{exc}")
        finally:
            self.monitoring = False
            try:
                self._service.close()
            except Exception:
                pass

    def shutdown(self) -> None:
        """请求中断（程序退出 / 重新登录时调用）。"""
        if self._service is not None:
            self._service.cancel()

    def close_browser(self) -> None:
        """尽力关闭浏览器（在线程结束后调用）。"""
        if self._service is not None:
            try:
                self._service.close()
            except Exception:
                pass
