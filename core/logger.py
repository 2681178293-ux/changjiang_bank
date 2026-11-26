# -*- coding: utf-8 -*-
"""统一日志：控制台 + 任意订阅者（GUI 用它把日志写进日志框）。

用法：
    logger = get_logger("登录")
    logger.add_sink(ui_append)      # 每次输出都会回调 ui_append("[10:33:00] [登录] xxx")
    logger.info("开始识别 U盾")
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Iterable

Sink = Callable[[str], None]

# 全局订阅者：所有 logger 的输出都会推给它们
_GLOBAL_SINKS: list[Sink] = []
_LOCK = threading.Lock()

# 详细日志开关：默认关闭，debug() 不输出（界面只保留关键信息）。
# 排查问题时可在代码里调用 set_verbose(True) 打开全部过程日志。
_VERBOSE = False


def set_verbose(enabled: bool) -> None:
    global _VERBOSE
    _VERBOSE = bool(enabled)


def bind_global_sink(sink: Sink) -> None:
    """注册一个全局日志接收器（GUI 只调一次即可）。"""
    with _LOCK:
        if sink not in _GLOBAL_SINKS:
            _GLOBAL_SINKS.append(sink)


def unbind_global_sink(sink: Sink) -> None:
    with _LOCK:
        if sink in _GLOBAL_SINKS:
            _GLOBAL_SINKS.remove(sink)


class Logger:
    """带标签的轻量日志器，输出格式：[HH:MM:SS] [标签] 内容"""

    def __init__(
        self,
        tag: str = "",
        sinks: Iterable[Sink] | None = None,
        echo: bool = True,
    ) -> None:
        self.tag = tag
        self._sinks: list[Sink] = list(sinks or ())
        self._echo = echo

    # ---------- 订阅 ----------
    def add_sink(self, sink: Sink) -> None:
        if sink not in self._sinks:
            self._sinks.append(sink)

    def child(self, tag: str) -> "Logger":
        """派生子标签，继承同样的接收器。"""
        return Logger(f"{self.tag}.{tag}" if self.tag else tag, self._sinks, self._echo)

    # ---------- 输出 ----------
    def _emit(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        if self.tag and level:
            line = f"[{stamp}] [{self.tag}] {level}：{message}"
        elif self.tag:
            line = f"[{stamp}] [{self.tag}] {message}"
        elif level:
            line = f"[{stamp}] [{level}] {message}"
        else:
            line = f"[{stamp}] {message}"
        if self._echo:
            try:
                print(line, file=sys.stderr, flush=True)
            except Exception:
                pass
        for sink in list(self._sinks) + list(_GLOBAL_SINKS):
            try:
                sink(line)
            except Exception:
                # 日志本身不允许抛异常影响主流程
                pass

    def info(self, message: str) -> None:
        self._emit("", message)

    def warn(self, message: str) -> None:
        self._emit("警告", message)

    def error(self, message: str) -> None:
        self._emit("错误", message)

    def debug(self, message: str) -> None:
        if _VERBOSE:
            self._emit("调试", message)


def get_logger(tag: str = "") -> Logger:
    return Logger(tag)
