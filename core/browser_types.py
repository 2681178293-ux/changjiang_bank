# -*- coding: utf-8 -*-
"""浏览器层公共类型：结果模型、异常、等待结束原因。

从旧的 Playwright 封装（core/browser.py，已删除）中抽出，供 edge_browser
与 service 共用。只放数据结构，不含任何浏览器实现。
"""
from __future__ import annotations

from dataclasses import dataclass


class BrowserError(RuntimeError):
    """浏览器启动 / 操作失败。"""


# wait_login 的结束原因
KIND_CANCELLED = "cancelled"
KIND_BROWSER_CLOSED = "browser_closed"
KIND_UKEY_REMOVED = "ukey_removed"
KIND_TIMEOUT = "timeout"


@dataclass
class LoginProbe:
    """一次登录状态探测结果。"""

    ok: bool
    reason: str = ""
    kind: str = ""     # ""=还没登录成功，其余见上面的 KIND_*
