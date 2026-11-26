# -*- coding: utf-8 -*-
"""把「同步」放到后台线程，避免抓数时卡住 Qt 界面。

分层：本文件只做线程搬运 + 信号转发；真正的业务逻辑在 service/sync_service.py。
"""
from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QThread, Signal

from core.logger import Logger
from service.sync_service import SyncOutcome, SyncService


class SyncWorker(QThread):
    """一次同步任务的执行者（抓数 + 本地入库，不推送）。"""

    log_line = Signal(str)
    item_done = Signal(str, int, int, bool, str)  # kind, fetched, written, ok, message
    finished_all = Signal()

    def __init__(self, kinds: Iterable[str], begin: str, end: str, parent=None) -> None:
        super().__init__(parent)
        self.kinds = list(kinds)
        self.begin = begin
        self.end = end

    def run(self) -> None:  # noqa: D401 - QThread 入口
        logger = Logger("同步", sinks=[self.log_line.emit])
        service = SyncService(logger=logger)
        try:
            outcomes: list[SyncOutcome] = service.sync(self.kinds, self.begin, self.end)
            for o in outcomes:
                self.item_done.emit(o.kind, o.fetched, o.written, o.ok, o.message)
        except Exception as exc:  # 兜底：任何异常都要回报界面
            logger.error(f"同步流程异常：{exc}")
            self.item_done.emit("", 0, 0, False, str(exc))
        finally:
            self.finished_all.emit()
