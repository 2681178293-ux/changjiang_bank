# -*- coding: utf-8 -*-
"""江苏长江商业银行网银业务：目前只实现「登录」。

登录流程（全自动，无需人工输密码）：
    ① 识别 U盾  —— 先确认插入的是江苏长江商业银行 U盾（读 CSP 密钥容器），识别不到就停在这里，不打开浏览器；
    ② 进入登录页 —— 用本机 Microsoft Edge 打开企业网银登录页
                   （ebank.jindiancloud.com/ebank/prelogin.do）；
    ③ 弹窗监控   —— 起后台线程盯 U盾 口令弹窗与「选择证书」弹窗：自动输入口令、自动确认证书；
    ④ 自动填表   —— 等页面带出客户号/登录名后，用 UIA 定位密码框，
                   通过 Interception 驱动级输入填登录密码并点「登录」
                   （普通 keybd_event 带注入标志会被 ActiveX 拦截；驱动级扫描码可绕过）。
                   页面无验证码输入框，整条链路全自动。
    ⑤ 判定成功   —— 页面进入网银主界面后，日志输出「已经登录成功，可以进行后续数据同步操作」；
    ⑥ 登录后     —— 继续盯着浏览器和 U盾：关窗口 / 拔 U盾 都会立刻反映到界面与日志。

自动化开关在 config/credentials.json：
   auto_fill_password 关掉 = 不自动填网页密码（留给人工）；
   auto_pin 关掉           = 不自动输入 U盾 口令（弹窗留给人工）；
   auto_confirm_cert 关掉  = 不自动确认「选择证书」弹窗。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from config import feinuo_config
from config.settings import SETTINGS, Settings
from core.browser_types import (
    KIND_BROWSER_CLOSED,
    KIND_CANCELLED,
    KIND_UKEY_REMOVED,
    BrowserError,
)
from core.form_filler import FormFiller
from core.card_reader import UKeyReader
from core.cookie_reader import refresh_cookie
from service.account_discoverer import AccountDiscoverer
from core.credentials import load as load_credentials
from core.edge_browser import EdgeBrowser
from core.logger import Logger, get_logger
from core.ukey_pin import PopupWatcher

LOGIN_OK_MESSAGE = "已经登录成功，可以进行后续数据同步操作"
BROWSER_CLOSED_MESSAGE = "浏览器已关闭，请重新登录或退出"
UKEY_REMOVED_MESSAGE = "U盾已经拔出，请插入U盾后登录或退出"
UKEY_BACK_MESSAGE = "已重新识别到 U盾，可以重新登录"


@dataclass
class LoginResult:
    """登录结果。stage 标记卡在哪一步，便于 UI 与后续排查。"""

    ok: bool
    stage: str  # ukey / browser / click / wait / done / browser_closed / ukey_removed / cancelled
    message: str


class ChangjiangBankService:
    """江苏长江商业银行网银业务。当前仅提供登录，后续同步逻辑继续加在本类里。"""

    def __init__(self, settings: Settings = SETTINGS, logger: Logger | None = None) -> None:
        self._s = settings
        self._log = logger or get_logger("网银")
        self._reader = UKeyReader(settings)
        self._browser: EdgeBrowser | None = None
        self.logged_in = False
        self._ukey_csp = ""                 # 识别成功后记住 CSP 名，监控拔插时直接用
        self._stop = threading.Event()      # 中断监控/等待
        self._watcher: PopupWatcher | None = None   # 弹窗监控（口令框 / 证书框）

    # ---------- 对外属性 ----------
    @property
    def browser(self) -> EdgeBrowser | None:
        return self._browser

    @property
    def reader(self) -> UKeyReader:
        return self._reader

    @property
    def ukey_csp(self) -> str:
        return self._ukey_csp

    @property
    def browser_started(self) -> bool:
        return bool(self._browser and self._browser.is_running)

    # ---------- 登录 ----------
    def login(self, on_stage: Callable[[str], None] | None = None) -> LoginResult:
        def stage(name: str) -> None:
            if on_stage:
                on_stage(name)

        self._stop.clear()

        # 每次登录都重新读凭据/开关：设置页改完立刻生效，不用重启程序
        creds = load_credentials()
        self._log.debug(f"登录配置：{creds.masked()}")

        # ① 识别 U盾
        stage("ukey")
        self._log.info("正在识别 U盾…")
        ukey = self._reader.wait_for_ukey(on_tick=self._on_ukey_tick)
        if not ukey.present:
            self._log.error(f"U盾识别失败：{ukey.hint or '未检测到已插入的 U盾'}")
            return LoginResult(False, "ukey", ukey.hint or "未检测到 U盾")

        self._ukey_csp = ukey.csp or self._ukey_csp
        if ukey.certificate:
            self._log.debug(f"已识别 U盾：{ukey.certificate.summary()}")
        else:
            self._log.debug(f"已识别 U盾：{ukey.describe()}")
        self._log.info("✔ 已识别 U盾")

        # ② 打开网银
        stage("browser")
        self._log.info("正在打开企业网银…")
        if self._browser is None:
            self._browser = EdgeBrowser(self._s, self._log.child("浏览器"))
        try:
            self._browser.start()
            entry = self._browser.enter_corp_ebank()
        except BrowserError as exc:
            self._log.error(str(exc))
            return LoginResult(False, "browser", str(exc))

        self._log.debug(entry)

        # ③ 弹窗监控 + 人工输入
        #    弹窗监控要先起来：U盾 口令框可能由页面加载或安全控件随时弹出，
        #    起晚一步就会漏掉（漏掉 = 卡在那里等人输）。
        watcher = self._start_popup_watcher(creds)
        try:
            # ③ 自动填写登录密码（Interception 驱动级输入可绕过 ActiveX 安全控件）
            if creds.auto_fill_password:
                stage("fill")
                filler = FormFiller(self._log.child("填表"))
                result = filler.fill_password_and_login(
                    self._browser.hwnd,
                    creds.password,
                    abort_check=self._ukey_watch(),
                )
                if result.ok:
                    self._log.debug("登录密码已自动填入并提交")
                else:
                    self._log.warn(f"自动填写未完成：{result.message}；请手动填密码/点登录")
            else:
                self._log.info("自动填密码已关闭，请在浏览器里手动输入登录密码并点「登录」")

            # ④ 等待进入主界面（同时盯着浏览器窗口和 U盾）
            stage("wait")
            probe = self._browser.wait_login(abort_check=self._ukey_watch())
        finally:
            self._stop_popup_watcher(watcher)

        if probe.ok:
            self.logged_in = True
            self._log.debug(probe.reason)

            # 登录成功后自动从浏览器内存读取会话 Cookie 并写回配置，
            # 使用者无需再开 F12 手动复制（读不到也不影响登录本身）。
            try:
                cookie, values = refresh_cookie()
                if cookie:
                    self._log.debug(
                        f"✔ 已自动获取登录态 Cookie（{len(values)} 项）"
                    )
                else:
                    self._log.warn(
                        "未能自动读取 Cookie；若同步失败，请登录后在 F12 手动复制 Cookie 到 config/query_runtime.json"
                    )
            except Exception as exc:
                self._log.warn(f"自动读取 Cookie 失败（不影响登录）：{exc}")

            # 登录成功后自动发现当前 U盾 授权的账号并写回配置，
            # 换公司U盾不用手动改 account_no。失败不覆盖已有账号。
            try:
                self._refresh_account()
            except Exception as exc:
                self._log.warn(f"自动发现账号失败（不影响登录）：{exc}")

            # 提示放在最后：确保用户看到"可以同步"时，Cookie/账号都已写回配置，
            # 否则用户立刻点同步会用到上一次的旧会话、导致抓 0 条。
            self._log.info(LOGIN_OK_MESSAGE)
            return LoginResult(True, "done", LOGIN_OK_MESSAGE)

        if probe.kind == KIND_BROWSER_CLOSED:
            self.logged_in = False
            self._log.warn(BROWSER_CLOSED_MESSAGE)
            return LoginResult(False, "browser_closed", BROWSER_CLOSED_MESSAGE)

        if probe.kind == KIND_UKEY_REMOVED:
            self.logged_in = False
            self._log.warn(UKEY_REMOVED_MESSAGE)
            return LoginResult(False, "ukey_removed", UKEY_REMOVED_MESSAGE)

        if probe.kind == KIND_CANCELLED:
            return LoginResult(False, "cancelled", "已取消")

        self._log.error(f"登录未完成：{probe.reason}")
        return LoginResult(False, "wait", probe.reason)

    def _refresh_account(self) -> None:
        """登录成功后从网银准备页发现当前U盾的全部账号与本方公司名，写回配置。

        账号写入 account_nos（account_no 保留首个兼容），公司名写入飞诺 ownerCompany。
        """
        import json
        from core.query_client import RUNTIME_PATH

        discoverer = AccountDiscoverer(logger=self._log.child("账号"))
        accounts, company = discoverer.discover_with_company()
        if not accounts:
            self._log.warn("未能自动发现账号；如同步失败请手动填写 config/query_runtime.json 的 account_nos")
            return
        data = {}
        if RUNTIME_PATH.exists():
            raw = RUNTIME_PATH.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            try:
                data = json.loads(raw.decode("utf-8", errors="ignore"))
            except json.JSONDecodeError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        old = data.get("account_nos") or [data.get("account_no")]
        data["account_nos"] = accounts
        data["account_no"] = accounts[0]
        RUNTIME_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        changed = list(old) != list(accounts)
        extra = f"，将逐个查询" if len(accounts) > 1 else ""
        tail = ', '.join('…' + a[-4:] for a in accounts)
        self._log.debug(f"账号识别明细：{tail}{extra}" + ("（已更新）" if changed else ""))
        self._log.info(f"✔ 已识别账号：{tail}{extra}")

        # 本方公司名写回飞诺 ownerCompany，推送时自动带上（换公司U盾自动适配）
        if company:
            try:
                if feinuo_config.set_owner_company(company):
                    self._log.info(f"✔ 已识别本方公司：{company}")
                else:
                    self._log.warn("写入本方公司名失败")
            except Exception as exc:
                self._log.warn(f"写入本方公司名失败（不影响登录）：{exc}")
        else:
            self._log.warn("未从网银识别到本方公司名；「本公司」列可能为空，可在 erp_config.json 的 ownerCompany 手动填")

    # ---------- 自动登录：弹窗监控 ----------
    def _start_popup_watcher(self, creds) -> PopupWatcher | None:
        """起后台线程盯 U盾 口令弹窗 / 证书选择弹窗。"""
        if not (creds.auto_pin or creds.auto_confirm_cert):
            self._log.debug("自动输入已关闭：请在弹窗里手动输入 U盾 口令、手动选证书")
            return None
        if creds.auto_pin and not creds.ukey_pin:
            self._log.warn("还没配置 U盾 口令，自动输入会跳过（「设置 · 登录」里可以填）")

        watcher = PopupWatcher(
            self._log.child("弹窗"),
            pin_getter=lambda: load_credentials().ukey_pin,
            settings=self._s,
            auto_pin=creds.auto_pin,
            auto_confirm_cert=creds.auto_confirm_cert,
        )
        watcher.prime()      # 记住当前已有窗口，之后只认新弹出的
        watcher.start()
        self._watcher = watcher
        self._log.debug(
            "已启动弹窗监控："
            + ("自动输入 U盾 口令" if creds.auto_pin else "")
            + ("、" if creds.auto_pin and creds.auto_confirm_cert else "")
            + ("自动确认选择证书" if creds.auto_confirm_cert else "")
        )
        return watcher

    def _stop_popup_watcher(self, watcher: PopupWatcher | None) -> None:
        if watcher is not None:
            watcher.stop()
            self._log.debug(f"弹窗处理汇总：{watcher.stats.summary()}")
        self._watcher = None

    # ---------- 登录成功后：会话监控 ----------
    def watch_session(self, on_event: Callable[[str, str], None]) -> None:
        """盯着已登录的会话：浏览器被关掉、U盾 被拔出，各上报一次。

        轮询窗口标题与 U盾 在位状态，边沿触发只报一次。
        退出条件：浏览器已关闭，或 cancel()。
        """
        browser = self._browser
        if browser is None:
            return

        poll = self._s.monitor_poll_seconds
        ukey_out = False
        next_ukey = 0.0
        while not self._stop.is_set():
            if not browser.is_alive():
                self.logged_in = False
                on_event(KIND_BROWSER_CLOSED, BROWSER_CLOSED_MESSAGE)
                return

            now = time.time()
            if now >= next_ukey:
                next_ukey = now + self._s.ukey_check_seconds
                present = self._reader.quick_present(self._ukey_csp)
                if present is False and not ukey_out:
                    ukey_out = True
                    self.logged_in = False
                    on_event(KIND_UKEY_REMOVED, UKEY_REMOVED_MESSAGE)
                elif present is True and ukey_out:
                    ukey_out = False
                    on_event("ukey_back", UKEY_BACK_MESSAGE)

            self._stop.wait(poll)

    def _ukey_watch(self, interval: float | None = None) -> Callable[[], str]:
        """登录等待期间顺手盯 U盾：中途拔掉就立刻结束等待。"""
        interval = self._s.ukey_check_seconds if interval is None else interval
        state = {"next": time.time() + interval}

        def check() -> str:
            now = time.time()
            if now < state["next"]:
                return ""
            state["next"] = now + interval
            if self._reader.quick_present(self._ukey_csp) is False:
                return UKEY_REMOVED_MESSAGE
            return ""

        return check

    # ---------- U盾检测过程日志 ----------
    def _on_ukey_tick(self, info, remaining: int) -> None:
        if info.present:
            return
        if info.is_target_bank:
            self._log.warn(f"{info.hint}（还剩 {remaining} 秒）")
        else:
            self._log.warn(f"{info.hint}（还剩 {remaining} 秒）")

    # ---------- 清理 ----------
    def cancel(self) -> None:
        """中断正在等待的登录 / 会话监控（程序退出或重新登录时用）。"""
        self._stop.set()
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        if self._browser is not None:
            self._browser.cancel()

    def close(self) -> None:
        self._stop.set()
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        self.logged_in = False
