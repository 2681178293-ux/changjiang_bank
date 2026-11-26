# -*- coding: utf-8 -*-
"""Win32 窗口 / 键盘 / 鼠标基础能力（纯标准库 ctypes，无第三方依赖）。

为什么需要这一层：
    长江的登录密码框、证书选择框、U盾 PIN 框都是**操作系统原生窗口**
    （网页里的密码框是银行 ActiveX 安全控件渲染的），
    Playwright / CDP 完全看不到，只能在 Win32 层找窗口 → 抢焦点 → 发真实按键。

本模块只提供"能力"，不含任何业务判断：
    · 窗口枚举 / 属性读取（标题、类名、进程、矩形、子控件）
    · 激活窗口、强制前台（SetForegroundWindow 常被系统拒绝，用鼠标点标题栏绕过）
    · 真实鼠标点击、真实按键注入（keybd_event）
    · INPUT_LOCK：全局输入锁，保证"同一时刻只有一个人在点鼠标/敲键盘"
      （表单自动填写与 U盾 PIN 注入跑在不同线程，必须互斥）

参考实现在农行工具里已实测通过（keybd_event 注入 + 焦点/存活守卫），
这里做了通用化，去掉了农行专用的窗口标题与 K宝 细节。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import random
import threading
import time
from dataclasses import dataclass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 64 位下涉及指针/句柄参数必须显式声明类型，否则会被按 32 位 int 截断
user32.CallNextHookEx.restype = wt.LPARAM
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]

_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
_EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

KEYEVENTF_KEYUP = 0x0002
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_DELETE = 0x2E
VK_A = 0x41
VK_BACK = 0x08

# 「同一时刻只有一个人在操作键鼠」——表单填写（主线程）与 PIN 注入（监控线程）互斥
INPUT_LOCK = threading.RLock()

# 表单填写进行中：U盾 口令/证书弹窗在这期间出现时，监控线程先让路，
# 否则它的注入会抢走焦点，剩下的表单字符就可能被打进弹窗口令框里（很危险）
FORM_FILLING = threading.Event()


@dataclass
class WindowInfo:
    """一个顶层窗口的快照。"""

    hwnd: int
    title: str = ""
    cls: str = ""
    pid: int = 0
    exe: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    visible: bool = False

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    def describe(self) -> str:
        return (
            f"句柄={self.hwnd} 类名={self.cls!r} 标题={self.title.strip()!r} "
            f"尺寸={self.width}x{self.height}"
        )


# ------------------------------------------------------------
# 读取窗口属性
# ------------------------------------------------------------
def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_exe(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def is_visible(hwnd: int) -> bool:
    try:
        return bool(user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def is_alive(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def describe_window(hwnd: int) -> WindowInfo:
    pid = window_pid(hwnd)
    return WindowInfo(
        hwnd=hwnd,
        title=window_title(hwnd),
        cls=window_class(hwnd),
        pid=pid,
        exe=process_exe(pid) if pid else "",
        rect=window_rect(hwnd),
        visible=is_visible(hwnd),
    )


# ------------------------------------------------------------
# 枚举窗口
# ------------------------------------------------------------
def top_windows(visible_only: bool = False) -> list[int]:
    """枚举全部顶层窗口（含被拥有的对话框）。"""
    found: list[int] = []

    def cb(hwnd, _lparam):
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        found.append(hwnd)
        return True

    user32.EnumWindows(_EnumWindowsProc(cb), 0)
    return found


def snapshot_windows() -> set[int]:
    """当前所有可见顶层窗口句柄集合，用于识别"新弹出的窗口"。"""
    return set(top_windows(visible_only=True))


def find_window_prefix(prefix: str) -> int:
    """按标题前缀找可见顶层窗口；找不到返回 0。"""
    for hwnd in top_windows(visible_only=True):
        if window_title(hwnd).strip().startswith(prefix):
            return hwnd
    return 0


def find_window_contains(keyword: str) -> int:
    for hwnd in top_windows(visible_only=True):
        if keyword in window_title(hwnd):
            return hwnd
    return 0


def list_windows_contains(keyword: str) -> list[WindowInfo]:
    out: list[WindowInfo] = []
    for hwnd in top_windows(visible_only=True):
        if keyword in window_title(hwnd):
            out.append(describe_window(hwnd))
    return out


def find_window_by_title(prefixes: tuple[str, ...] | list[str]) -> int:
    for prefix in prefixes:
        hwnd = find_window_prefix(prefix)
        if hwnd:
            return hwnd
    return 0


# ------------------------------------------------------------
# 子控件
# ------------------------------------------------------------
def child_windows(hwnd: int) -> list[int]:
    found: list[int] = []

    def cb(child, _lparam):
        found.append(child)
        return True

    user32.EnumChildWindows(hwnd, _EnumChildProc(cb), 0)
    return found


def find_child_by_class(hwnd: int, class_name: str) -> int:
    target = class_name.lower()
    for child in child_windows(hwnd):
        if window_class(child).lower() == target:
            return child
    return 0


def find_children_by_class(hwnd: int, class_name: str) -> list[int]:
    target = class_name.lower()
    return [c for c in child_windows(hwnd) if window_class(c).lower() == target]


def find_child_button(hwnd: int, text_key: str) -> int:
    """找文本含 text_key 的 Button 子控件（标准对话框用）。"""
    for child in child_windows(hwnd):
        if window_class(child).lower() != "button":
            continue
        text = window_title(child)
        if text and text_key in text:
            return child
    return 0


def children_classes(hwnd: int) -> list[tuple[int, str, int]]:
    """[(句柄, 类名, 标题长度)]，诊断用。"""
    return [(c, window_class(c), len(window_title(c))) for c in child_windows(hwnd)]


# ------------------------------------------------------------
# 焦点 / 激活 / 点击
# ------------------------------------------------------------
def activate_window(hwnd: int, settle: float = 0.4) -> None:
    """激活并置顶（注入前必需）。"""
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE：防最小化抢焦点失败
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetActiveWindow(hwnd)
    time.sleep(settle)


# ------------------------------------------------------------
# 强制前台（SetForegroundWindow 会被系统拒绝，这里三重兜底）
# ------------------------------------------------------------
SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
SPIF_SENDCHANGE = 0x0002


def _foreground_lock_timeout() -> int:
    value = wt.DWORD()
    user32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(value), 0)
    return value.value


def ensure_foreground(hwnd: int, settle: float = 0.25, tries: int = 3) -> bool:
    """确保 hwnd 真的是前台窗口，返回是否成功。

    为什么不能用 SetForegroundWindow 一把梭：
      Windows 只允许「当前前台进程 / 刚收到用户输入的进程」切换前台，
      后台进程调用会被静默忽略（返回 True 但不生效）。这里按成功率依次尝试：
        ① 临时把前台锁定超时设为 0 + AttachThreadInput 到当前前台线程，再 SetForegroundWindow；
        ② 仍然不行就点一下标题栏（系统把鼠标点击视为用户意图，必定放行）；
        ③ 最小化的先 SW_RESTORE。
    只有 GetForegroundWindow() 真的等于 hwnd 才算成功 —— 后续按键才敢发。
    """
    if not is_alive(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        time.sleep(0.2)
    if foreground_window() == hwnd:
        return True

    for _ in range(max(1, tries)):
        previous = _foreground_lock_timeout()
        try:
            user32.SystemParametersInfoW(
                SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), SPIF_SENDCHANGE
            )
            fg = foreground_window()
            fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            cur_tid = kernel32.GetCurrentThreadId()
            attached = False
            if fg_tid and fg_tid != cur_tid:
                attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
            try:
                user32.ShowWindow(hwnd, 9)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.SetFocus(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)
        except Exception:
            pass
        finally:
            try:
                user32.SystemParametersInfoW(
                    SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                    ctypes.c_void_p(previous), SPIF_SENDCHANGE,
                )
            except Exception:
                pass

        time.sleep(settle)
        if foreground_window() == hwnd:
            return True

        # 兜底：点标题栏中部（离最小化/关闭按钮远一点）
        rect = wt.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            x = (rect.left + rect.right) // 2
            y = rect.top + 12
            if y < 0:
                y = rect.top + 30
            set_cursor(x, y)
            mouse_click()
            time.sleep(settle)
            if foreground_window() == hwnd:
                return True

    return foreground_window() == hwnd


def top_level_of(hwnd: int) -> int:
    """沿父链找到顶层窗口（WindowFromPoint 可能返回子窗口）。"""
    current = hwnd
    for _ in range(16):
        parent = user32.GetAncestor(current, 2)     # GA_ROOT
        if not parent or parent == current:
            return current
        current = parent
    return current


def window_at(x: int, y: int) -> int:
    """屏幕坐标处最上层窗口（顶层）。"""
    hwnd = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
    return top_level_of(hwnd) if hwnd else 0


def point_reaches_window(hwnd: int, x: int, y: int) -> bool:
    """坐标处最上层的窗口是不是目标窗口（或被目标窗口拥有）。

    防的是最坏情况：目标窗口被别的程序挡住/最小化时，盲点鼠标会把点击
    送给别人，接下来的密码字符就会打进不相干的输入框。
    """
    top = window_at(x, y)
    if not top:
        return False
    if top == hwnd:
        return True
    # 弹窗（比如控件自绘的子窗口）可能属于同一进程，用 PID 兜一层
    return window_pid(top) == window_pid(hwnd)


def force_foreground_by_click(hwnd: int, settle: float = 0.3) -> bool:
    """鼠标点窗口标题栏中部，强制把窗口带到前台。

    Windows 只对"用户鼠标点击"放行前台切换，后台进程调 SetForegroundWindow
    常被静默拒绝；点击可绕过该限制，确保后续按键送达目标窗口。
    返回 True=点击后确实成了前台窗口。
    """
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    x = (rect.left + rect.right) // 2
    y = rect.top + max(12, (rect.bottom - rect.top) // 30)
    set_cursor(x, y)
    mouse_click()
    time.sleep(settle)
    return user32.GetForegroundWindow() == hwnd


def set_cursor(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def mouse_click(x: int | None = None, y: int | None = None) -> None:
    """左键单击（可指定屏幕坐标）。"""
    if x is not None and y is not None:
        set_cursor(x, y)
        time.sleep(0.08)
    user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP


def foreground_window() -> int:
    return user32.GetForegroundWindow()


# ------------------------------------------------------------
# 键盘注入（带守卫）
# ------------------------------------------------------------
def press_vk(vk: int, scan: int = 0) -> None:
    user32.keybd_event(vk, scan, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(vk, scan, KEYEVENTF_KEYUP, 0)


def press_enter() -> None:
    """注入 Enter（带标准扫描码 0x1C，兼容自绘窗口）。"""
    user32.keybd_event(VK_RETURN, 0x1C, 0, 0)
    time.sleep(0.1)
    user32.keybd_event(VK_RETURN, 0x1C, KEYEVENTF_KEYUP, 0)


def press_tab() -> None:
    press_vk(VK_TAB, 0x0F)


def press_ctrl_a() -> None:
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_A, 0, 0, 0)
    user32.keybd_event(VK_A, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)


def press_backspace(times: int = 1) -> None:
    for _ in range(max(0, times)):
        press_vk(VK_BACK, 0x0E)


def type_text(
    text: str,
    hwnd: int = 0,
    interval: tuple[float, float] = (0.03, 0.08),
) -> None:
    """逐字符注入文本（keybd_event）。大写字母走 Shift 组合。

    hwnd 非 0 时启动两道守卫（对齐农行工具已验证的做法）：
      · 存活守卫：每字符前确认窗口仍存在且可见，中途消失立刻中断；
      · 焦点守卫：每字符前确认前台窗口仍是它，用户切走立刻中断
        （防止剩余字符打进别的窗口 → 内容残缺 + 明文泄漏）。
    守卫失败抛 RuntimeError，由调用方捕获后转人工，绝不继续注入。
    """
    for index, ch in enumerate(text):
        if hwnd:
            if not is_alive(hwnd) or not is_visible(hwnd):
                raise RuntimeError(
                    f"注入第 {index + 1}/{len(text)} 个字符前窗口已关闭/不可见，已中断"
                )
            if foreground_window() != hwnd:
                raise RuntimeError(
                    f"注入第 {index + 1}/{len(text)} 个字符前焦点被切换，已中断"
                )
        _type_char(ch)
        time.sleep(random.uniform(*interval))


def _type_char(ch: str) -> None:
    if ch.isalpha() and ch.isascii():
        vk = 0x41 + ord(ch.lower()) - ord("a")
        if ch.isupper():
            user32.keybd_event(VK_SHIFT, 0, 0, 0)
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
        else:
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        return
    if ch.isdigit():
        vk = 0x30 + int(ch)
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        return
    if ch == " ":
        user32.keybd_event(0x20, 0, 0, 0)
        user32.keybd_event(0x20, 0, KEYEVENTF_KEYUP, 0)
        return
    raise ValueError(f"暂不支持注入的字符：{ch!r}（只支持字母/数字/空格）")


# ------------------------------------------------------------
# 文本注入（SendInput + KEYEVENTF_UNICODE）
# ------------------------------------------------------------
ULONG_PTR = ctypes.c_size_t
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUTUNION)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]


def _send_unicode_char(ch: str) -> None:
    code = ord(ch)
    for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
        inp = _INPUT(type=INPUT_KEYBOARD)
        inp.union.ki = _KEYBDINPUT(
            wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=0
        )
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def type_text_unicode(
    text: str,
    hwnd: int = 0,
    interval: tuple[float, float] = (0.02, 0.06),
) -> None:
    """按 Unicode 码位直接注入字符（SendInput + KEYEVENTF_UNICODE）。

    相比 keybd_event 按虚拟键码模拟，这里绕开了**键盘布局和输入法**：
    本机默认装的是搜狗拼音，中文状态下用 VK 模拟敲数字/字母会被输入法
    截去做候选词（页面里一个字都进不去，形如"填了但读不到"）。
    Unicode 注入等价于直接送 WM_CHAR，银行安全控件、IE 内嵌框都能收到。

    守卫与 type_text 相同：hwnd 非 0 时逐字符校验存活 + 前台焦点。
    """
    for index, ch in enumerate(text):
        if hwnd:
            if not is_alive(hwnd) or not is_visible(hwnd):
                raise RuntimeError(
                    f"注入第 {index + 1}/{len(text)} 个字符前窗口已关闭/不可见，已中断"
                )
            if foreground_window() != hwnd:
                raise RuntimeError(
                    f"注入第 {index + 1}/{len(text)} 个字符前焦点被切换，已中断"
                )
        _send_unicode_char(ch)
        time.sleep(random.uniform(*interval))


# ------------------------------------------------------------
# 轮询等待
# ------------------------------------------------------------
def wait_window(prefixes: tuple[str, ...] | list[str], timeout: float, poll: float = 0.3) -> int:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        hwnd = find_window_by_title(prefixes)
        if hwnd:
            return hwnd
        time.sleep(poll)
    return 0


def wait_window_closed(hwnd: int, timeout: float, poll: float = 0.3) -> bool:
    """等窗口消失（= 提交被接受）。"""
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not is_alive(hwnd) or not is_visible(hwnd):
            return True
        time.sleep(poll)
    return False
