# -*- coding: utf-8 -*-
"""Interception 驱动级输入验证工具（最小闭环，不改现有登录代码）。

目的：验证"驱动级键盘注入"能否进入目标输入框——先过基础输入关，
再确认能否绕过银行 ActiveX 安全控件。

原理：
    Interception 是内核过滤驱动，发出的按键不带 LLKHF_INJECTED 标志，
    安全控件会当成真人按键。库内部 write() 逐字符发硬件扫描码。

重要行为说明：
    · 本脚本**不调用 set_filter**，所以不会拦截你的真实键盘——
      脚本运行时你照样能打字，不会"键盘失灵"。
    · 真正会导致键盘失灵的是 set_filter + 不转发；本脚本不涉及。

用法（在项目根目录执行）：
    # 1) 自检：驱动是否加载、键盘设备号是多少
    .venv\\Scripts\\python.exe tools\\test_hid_input.py --check

    # 2) 列出探测到的设备
    .venv\\Scripts\\python.exe tools\\test_hid_input.py --devices

    # 3) 发测试串到"当前焦点窗口"（先打开记事本，把光标放进去）
    .venv\\Scripts\\python.exe tools\\test_hid_input.py --send "test1234" --delay 5

    # 4) 实测网银密码框：手动打开登录页、光标点进密码框后运行
    .venv\\Scripts\\python.exe tools\\test_hid_input.py --send "test1234" --delay 8

    # 5) 若怀疑设备号选错导致丢字，手动指定键盘设备号（用 --devices 看候选）
    .venv\\Scripts\\python.exe tools\\test_hid_input.py --send "test1234" --kb 2 --delay 8

注意：
    · 大小写：库不支持大写，大写字母会变成小写。测试串用小写+数字。
    · 千万别发真实密码做测试！用 test1234 这种假串。
    · 网银实测属于风控敏感行为，先用非生产账号。
    · 库的自动设备选择不稳定（可能在 ACPI 虚拟设备/真实键盘间乱跳），
      本脚本默认优先选真实物理 HID 键盘；若仍丢字，用 --kb 手动固定设备号。
"""
from __future__ import annotations

import argparse
import sys
import time


def _import_interception():
    try:
        import interception
        return interception
    except Exception as exc:  # DriverNotFoundError / ImportError 等
        print(f"[导入失败] {type(exc).__name__}: {exc}")
        print("  若提示 DriverNotFoundError：驱动未加载，确认已重启且装成功。")
        print("  若提示 No module named 'interception'：先 pip install interception-python pywin32")
        return None


def check_driver(ic) -> bool:
    """自检：驱动是否真的加载。"""
    try:
        kb = ic.get_keyboard()
        print(f"[OK] 驱动已加载，当前键盘设备号 = {kb}")
        return True
    except Exception as exc:
        print(f"[FAIL] 驱动未就绪：{type(exc).__name__}: {exc}")
        print("  请确认：安装器报了成功 + 已重启电脑。")
        return False


def list_devices(ic) -> None:
    """列出探测到的输入设备（HWID）。"""
    ic.auto_capture_devices(keyboard=True, mouse=True, verbose=True)
    print(f"[当前设备] 键盘={ic.get_keyboard()} 鼠标={ic.get_mouse()}")


def pick_keyboard(ic, kb_override: int | None = None) -> int:
    """选一个真实物理键盘设备号；跳过 ACPI 虚拟设备（会丢字）和 DD 虚拟键盘。

    优先级：真实 HID 键盘(HID\\VID_...) > DD 虚拟键盘(ddhid) > 其他 > ACPI。
    """
    if kb_override is not None:
        ic.set_devices(keyboard=kb_override)
        print(f"[设备] 手动指定键盘设备号 = {kb_override}")
        return kb_override

    ic.auto_capture_devices(keyboard=True, mouse=False)
    # 访问库内部上下文，读取每个设备的 HWID 来挑真实键盘
    try:
        from interception import inputs as _inputs
        ctx = _inputs._g_context
        best_num, best_score, best_hwid = None, -1, ""
        for n in range(0, 10):
            hwid = ctx.devices[n].get_HWID()
            if not hwid or not ctx.is_keyboard(n):
                continue
            hwid_u = hwid.upper()
            if "ACPI" in hwid_u:
                score = 0            # ACPI 虚拟设备，最后才选
            elif "DDHID" in hwid_u:
                score = 1            # DD 虚拟键盘，次选
            elif "VID_" in hwid_u:
                score = 2            # 真实物理 HID 键盘，首选
            else:
                score = 1
            if score > best_score:
                best_num, best_score, best_hwid = n, score, hwid
        if best_num is not None:
            ic.set_devices(keyboard=best_num)
            short = best_hwid.split("\x00")[0][:50]
            print(f"[设备] 自动选择键盘设备号 = {best_num}  HWID={short}")
            return best_num
    except Exception as exc:
        print(f"[设备] 自动挑选失败({exc})，退回默认设备号")

    kb = ic.get_keyboard()
    print(f"[设备] 使用键盘设备号 = {kb}（默认）")
    return kb


def send_text(ic, text: str, delay: int, interval: float, kb_override: int | None = None) -> None:
    """选好真实键盘设备后，倒计时，然后把文本发到当前焦点窗口。"""
    pick_keyboard(ic, kb_override)

    if delay > 0:
        print(f"[准备] {delay} 秒后开始发送——现在把光标点到目标输入框……")
        for i in range(delay, 0, -1):
            print(f"  {i}", flush=True)
            time.sleep(1)

    print(f"[发送] {text!r}（逐字符，间隔 {interval}s）")
    try:
        ic.write(text, interval=interval)
        print("[完成] 已发送。看看目标输入框里出现了什么。")
    except Exception as exc:
        print(f"[发送失败] {type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Interception 驱动级输入验证")
    parser.add_argument("--check", action="store_true", help="自检驱动是否加载")
    parser.add_argument("--devices", action="store_true", help="列出探测到的输入设备")
    parser.add_argument("--send", metavar="TEXT", help="发送指定文本到当前焦点窗口")
    parser.add_argument("--delay", type=int, default=5, metavar="SEC",
                        help="发送前倒计时秒数，留时间切焦点（默认 5）")
    parser.add_argument("--interval", type=float, default=0.08, metavar="SEC",
                        help="字符间隔秒数（默认 0.08）")
    parser.add_argument("--kb", type=int, default=None, metavar="NUM",
                        help="手动指定键盘设备号（丢字时用 --devices 看候选）")
    args = parser.parse_args()

    ic = _import_interception()
    if ic is None:
        return 2

    if args.check:
        return 0 if check_driver(ic) else 1
    if args.devices:
        list_devices(ic)
        return 0
    if args.send is not None:
        if not check_driver(ic):
            return 1
        send_text(ic, args.send, args.delay, args.interval, args.kb)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
