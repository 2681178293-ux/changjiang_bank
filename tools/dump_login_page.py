# -*- coding: utf-8 -*-
"""诊断小工具：把网银相关窗口 / 弹窗的窗口信息打出来。

用途（按需选一条跑）：
    1) 看当前所有窗口（不知道 U盾 口令弹窗标题时）：
           .venv\\Scripts\\python.exe tools\\dump_login_page.py --windows
    2) 盯着新弹出的窗口（点「登录」前后各跑 30 秒，把弹出的窗口抓下来）：
           .venv\\Scripts\\python.exe tools\\dump_login_page.py --watch 60

拿到标题后填到 config/.env 的 UKEY_PIN_WINDOW_TITLES。

注意：页面元素（UIA 树）诊断已随「表单自动填写」一起下架——登录信息由人工输入，
程序只保留窗口级的弹窗监控与诊断。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import SETTINGS                      # noqa: E402
from core import win32_ui as w32                          # noqa: E402
from core.logger import Logger                            # noqa: E402


def find_bank_window() -> int:
    """按窗口标题里的银行域名/银行名找浏览器窗口。"""
    hosts = tuple(h.lower() for h in SETTINGS.ebank_hosts)
    for hwnd in w32.top_windows(visible_only=True):
        if w32.window_class(hwnd) not in ("Chrome_WidgetWin_1", "IEFrame"):
            continue
        title = w32.window_title(hwnd).lower()
        if any(host in title for host in hosts) or SETTINGS.bank_name in title:
            return hwnd
    return 0


def dump_windows(log: Logger) -> None:
    log.info("—— 当前可见顶层窗口 ——")
    for hwnd in w32.top_windows(visible_only=True):
        info = w32.describe_window(hwnd)
        if not info.title.strip():
            continue
        edits = len(w32.find_children_by_class(hwnd, "edit"))
        extra = f" 输入框={edits}" if edits else ""
        log.info(f"  0x{hwnd:X} {info.describe()}{extra}")


def watch_windows(log: Logger, seconds: int) -> None:
    log.info(f"开始监控新窗口（{seconds} 秒）——现在去浏览器里点「登录」/ 插 U盾")
    baseline = w32.snapshot_windows()
    seen: set[int] = set()
    end = time.time() + seconds
    while time.time() < end:
        for hwnd in w32.top_windows(visible_only=True):
            if hwnd in baseline or hwnd in seen:
                continue
            seen.add(hwnd)
            info = w32.describe_window(hwnd)
            edits = w32.find_children_by_class(hwnd, "edit")
            classes = "、".join(sorted({w32.window_class(c) for c in w32.child_windows(hwnd)}))
            log.info(
                f"新窗口 0x{hwnd:X} {info.describe()} 输入框={len(edits)} 子控件类名=[{classes}]"
            )
        time.sleep(0.3)
    log.info(f"监控结束，共发现 {len(seen)} 个新窗口")


def print_usage(log: Logger) -> None:
    log.info("页面 UIA 诊断已下架（表单不再自动填写），现在只用窗口级诊断：")
    log.info("  --windows   列出当前所有可见窗口")
    log.info("  --watch 60  监控 60 秒内新弹出的窗口（抓 U盾 口令/证书弹窗标题用）")


def main() -> int:
    parser = argparse.ArgumentParser(description="网银窗口/弹窗诊断")
    parser.add_argument("--windows", action="store_true", help="列出当前所有可见窗口")
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                        help="监控新出现的窗口（抓弹窗标题用）")
    args = parser.parse_args()

    log = Logger("诊断")
    if args.watch:
        watch_windows(log, args.watch)
    elif args.windows:
        dump_windows(log)
    else:
        print_usage(log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
