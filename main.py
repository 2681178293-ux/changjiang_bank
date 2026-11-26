# -*- coding: utf-8 -*-
"""
江苏长江商业银行工具 v1.0 —— 启动类与界面编排

职责：加载 ui/ 下由 Qt Designer 设计、pyside6-uic 生成的页面，打通页面跳转；
      收集界面参数（日期/勾选类型/分组）并启动登录、同步的后台 worker，
      把结果回显到界面。具体业务逻辑都在 service/ 与 core/，本文件不含抓数/入库/推送细节。

运行：
    python main.py

页面：
    0 主界面          1 设置·登录       2 设置·同步       3 设置·飞诺 API

跳转关系：
    主界面「设置」        -> 设置·登录
    设置页点页签          -> 对应的设置页面
    设置页「返回主界面」  -> 主界面
    主界面「退出」        -> 关闭程序
    日期右侧「...」       -> 弹出日历选择日期

登录与同步（业务实现详见 service/changjiang_service.py 与 service/sync_service.py）：
登录：
    主界面「登录」 -> 识别江苏长江商业银行 U盾 -> 打开企业网银登录页
                  -> 等页面带出客户号/登录名 -> 自动填登录密码并点「登录」
                  -> 后台自动处理 U盾 口令弹窗与「选择证书」弹窗
                  -> 判定进入主界面后写日志
    密码自动填写用 Interception 驱动级输入绕过 ActiveX 安全控件（2026-11 实测通过）；
    普通 keybd_event 等用户态注入会被拦截。开关与凭据在 config/credentials.json。
"""
import sys

from PySide6.QtCore import QDate, QLocale, QPoint, Qt
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QStackedWidget, QLineEdit,
    QCalendarWidget,
)

from ui.ui_changjiang_01_main import Ui_ChangjiangMain
from ui.ui_changjiang_03_settings_login import Ui_ChangjiangSettingsLogin
from ui.ui_changjiang_04_settings_sync import Ui_ChangjiangSettingsSync
from ui.ui_changjiang_05_settings_feinuo_api import Ui_ChangjiangSettingsFeinuoApi
from ui.login_worker import LoginWorker
from ui.sync_worker import SyncWorker
from core.credentials import load as load_credentials, save as save_credentials
from config import feinuo_config
from service.changjiang_service import BROWSER_CLOSED_MESSAGE, UKEY_REMOVED_MESSAGE
from service.sync_service import SyncService

# (页面名, 界面类, 窗口尺寸)
PAGE_SPECS = [
    ("主界面", Ui_ChangjiangMain, (780, 740)),
    ("设置 · 登录", Ui_ChangjiangSettingsLogin, (480, 700)),
    ("设置 · 同步", Ui_ChangjiangSettingsSync, (480, 700)),
    ("设置 · 飞诺 API", Ui_ChangjiangSettingsFeinuoApi, (480, 700)),
]

class ChangjiangApp:
    """简单启动类：只装载页面 + 处理页面跳转。"""

    def __init__(self, argv=None):
        self.app = QApplication(argv if argv is not None else sys.argv)
        self.window = QWidget()
        self.window.setWindowTitle("江苏长江商业银行工具v1.0")
        self.window.resize(*PAGE_SPECS[0][2])

        root = QVBoxLayout(self.window)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        # 每个页面都是一个 QWidget，由对应的 Ui_ 类填充
        self.pages = []
        for _name, ui_class, _size in PAGE_SPECS:
            page = QWidget()
            ui = ui_class()
            ui.setupUi(page)
            self.stack.addWidget(page)
            self.pages.append(ui)

        self._calendar = None
        self._login_worker: LoginWorker | None = None
        self._sync_worker: SyncWorker | None = None
        self._logged_in = False
        self._loading_credentials = False   # 回填期间不回写，避免把刚读出来的值又存一遍
        self._loading_group = False         # 分组回填期间不触发存盘/日志
        self._bind()
        self._load_credentials()
        self._load_group()
        self.go(0)

    # ---------- 跳转 ----------
    def go(self, index: int):
        """跳转到指定页面（各页面尺寸不同，同步调整窗口大小）。"""
        self.stack.setCurrentIndex(index)
        self.window.resize(*PAGE_SPECS[index][2])

    # ---------- 日期选择 ----------
    @staticmethod
    def _parse_date(text: str) -> QDate:
        """兼容 2026-11-18 / 2026:11:18 / 2026/11/18 三种写法。"""
        raw = text.strip().replace(":", "-").replace("/", "-")
        for fmt in ("yyyy-M-d", "yyyy-MM-dd"):
            d = QDate.fromString(raw, fmt)
            if d.isValid():
                return d
        return QDate.currentDate()

    def _open_calendar(self, button, line_edit: QLineEdit):
        """在按钮下方弹出日历，选中后回填到输入框。"""
        cal = QCalendarWidget()
        cal.setWindowFlags(Qt.Popup)
        cal.setLocale(QLocale(QLocale.Chinese, QLocale.China))
        cal.setFirstDayOfWeek(Qt.Monday)
        cal.setGridVisible(True)
        cal.setSelectedDate(self._parse_date(line_edit.text()))
        cal.setFixedSize(300, 220)

        def on_picked(date: QDate):
            line_edit.setText(date.toString("yyyy-MM-dd"))
            cal.close()

        cal.clicked.connect(on_picked)
        cal.move(button.mapToGlobal(QPoint(0, button.height() + 2)))
        cal.show()
        self._calendar = cal  # 持有引用，避免被回收

    # ---------- 分组（主界面下拉 = 飞诺推送 groupName） ----------
    @staticmethod
    def _group_value(letter: str) -> int | None:
        """界面字母 A-E -> 飞诺 group 数字 1-5；无效返回 None。"""
        return feinuo_config.LETTER_GROUPS.get((letter or "").strip().upper())

    def current_group(self) -> int | None:
        """读取当前页面选中的分组数字（推送实际从 erp_config.json 取值，本方法备用）。"""
        return self._group_value(self.pages[0].main_cbo_group.currentText())

    def _on_group_changed(self, letter: str):
        if self._loading_group:
            return
        group = self._group_value(letter)
        if group is None:
            return
        if feinuo_config.set_group(group):
            self._log(f"[配置] 分组已设为 {letter.strip().upper()}（ERP group={group}），已保存")
        # 同步到三个设置页的「分组 ID」只读显示
        for ui in self._settings_uis():
            ui.api_input_group_id.setText(f"{letter.strip().upper()} 组（group={group}）")

    def _load_group(self):
        """启动时用配置里的 groupName 回填主界面下拉框。"""
        group = feinuo_config.get_group()
        if group is None:
            return  # 配置为空则保持界面默认（D）
        letter = feinuo_config.group_label(group)
        self._loading_group = True
        try:
            idx = self.pages[0].main_cbo_group.findText(letter)
            if idx >= 0:
                self.pages[0].main_cbo_group.setCurrentIndex(idx)
        finally:
            self._loading_group = False
        for ui in self._settings_uis():
            ui.api_input_group_id.setText(f"{letter} 组（group={group}）")

    # ---------- 信号绑定 ----------
    def _bind(self):
        main_ui = self.pages[0]

        # 主界面
        main_ui.main_btn_settings.clicked.connect(lambda: self.go(1))
        main_ui.main_btn_exit.clicked.connect(self._on_exit)
        main_ui.main_btn_login.clicked.connect(self._on_login)
        main_ui.main_btn_sync.clicked.connect(self._on_sync)

        # 分组下拉：改动即时存盘，并同步到三个设置页的「分组 ID」只读框
        main_ui.main_cbo_group.currentTextChanged.connect(self._on_group_changed)

        # 日期「...」按钮 -> 日历弹窗
        for button, line_edit in (
            (main_ui.main_btn_date_start_pick, main_ui.main_input_date_start),
            (main_ui.main_btn_date_end_pick, main_ui.main_input_date_end),
        ):
            button.clicked.connect(
                lambda _=False, b=button, e=line_edit: self._open_calendar(b, e)
            )

        # 三个设置页：返回 / 页签切换 / 显示密码 / 启用 API（均为纯界面联动）
        for index in (1, 2, 3):
            ui = self.pages[index]
            ui.set_btn_back.clicked.connect(lambda: self.go(0))
            ui.set_tab_widget.currentChanged.connect(
                lambda tab_index: self.go(1 + tab_index)
            )
            # 「显示密码」作用于 UKey PIN 输入框
            ui.login_chk_show.toggled.connect(
                lambda checked, u=ui: self._toggle_secret_echo(u, checked)
            )
            ui.api_chk_enable.toggled.connect(
                lambda checked, u=ui: self._toggle_api(u, checked)
            )
            # U盾口令/开关改动即时落盘：改完不用点保存，下次登录直接生效
            ui.login_input_pin.editingFinished.connect(self._save_credentials)
            ui.login_chk_auto_pin.toggled.connect(lambda _checked: self._save_credentials())

    # ---------- U盾口令与弹窗开关 ----------
    def _settings_uis(self):
        """三个设置页是同一份界面的副本，凭据要同步到全部页面。"""
        return self.pages[1:]

    def _active_settings_ui(self):
        index = self.stack.currentIndex()
        return self.pages[index] if index >= 1 else self.pages[1]

    def _load_credentials(self):
        creds = load_credentials()
        self._apply_credentials(creds)
        if not creds.ukey_pin:
            self._log("[提示] 未配置 U盾 口令，弹窗时需手动输入（设置 → 登录 可填写）")
        if creds.auto_fill_password and not creds.password:
            self._log("[提示] 未配置登录密码，自动填写会跳过（config/credentials.json 填 password）")

    def _apply_credentials(self, creds):
        self._loading_credentials = True
        try:
            for ui in self._settings_uis():
                ui.login_input_pin.setText(creds.ukey_pin)
                ui.login_chk_auto_pin.setChecked(bool(creds.auto_pin))
        finally:
            self._loading_credentials = False

    def _save_credentials(self):
        if self._loading_credentials:
            return
        ui = self._active_settings_ui()
        creds = load_credentials()          # 先读，保留 auto_confirm_cert 等界面没暴露的字段
        creds.ukey_pin = ui.login_input_pin.text().strip()
        creds.auto_pin = ui.login_chk_auto_pin.isChecked()
        if save_credentials(creds):
            self._log("[配置] U盾口令已保存（下次点「登录」生效）")
        else:
            self._log("[配置] U盾口令保存失败：请检查 config 目录是否可写")
        # 同步其它设置页的显示，避免切页后看到旧值
        for other in self._settings_uis():
            if other is ui:
                continue
            other.login_input_pin.setText(creds.ukey_pin)
            other.login_chk_auto_pin.setChecked(bool(creds.auto_pin))

    @staticmethod
    def _toggle_secret_echo(ui, checked: bool):
        mode = QLineEdit.Normal if checked else QLineEdit.Password
        ui.login_input_pin.setEchoMode(mode)

    # ---------- 仅 UI 反馈，无业务逻辑 ----------
    def _log(self, message: str):
        self.pages[0].main_log_area.appendPlainText(message)

    def _set_status(self, text: str):
        self.pages[0].main_lbl_status.setText(text)

    # ---------- 登录（真实流程：识别U盾 -> 打开网银 -> 用户输密码 -> 判定成功） ----------
    _STAGE_TEXT = {
        "ukey": "识别U盾中…",
        "browser": "正在打开企业网银…",
        "fill": "正在自动填写登录密码…",
        "wait": "等待进入网银主界面…",
    }

    def _on_login(self):
        worker = self._login_worker
        if worker is not None and worker.isRunning():
            if worker.monitoring:
                # 已登录、只是在盯会话：用户想重新登录 -> 先结束旧会话
                self._log("[提示] 先结束上一次会话，再重新登录…")
                worker.shutdown()
                worker.wait(5000)
                self._logged_in = False
            else:
                self._log("[提示] 登录流程正在进行中，请稍候…")
                return

        if self._logged_in:
            self._log("[提示] 当前已是登录状态；如需重新登录，请先在浏览器里点「安全退出」并关闭浏览器窗口")
            return

        self._set_status("登录中…")
        self.pages[0].main_btn_login.setEnabled(False)

        worker = LoginWorker()
        worker.log_line.connect(self._log)
        worker.stage_changed.connect(self._on_stage)
        worker.session_event.connect(self._on_session_event)
        worker.finished_with.connect(self._on_login_done)
        self._login_worker = worker
        worker.start()

    def _on_stage(self, stage: str):
        text = self._STAGE_TEXT.get(stage)
        if text:
            self._set_status(text)

    def _on_login_done(self, ok: bool, stage: str, message: str):
        self.pages[0].main_btn_login.setEnabled(True)
        self._logged_in = ok
        if ok:
            self._set_status("已登录")
        elif stage == "browser_closed":
            self._set_status(BROWSER_CLOSED_MESSAGE)
        elif stage == "ukey_removed":
            self._set_status(UKEY_REMOVED_MESSAGE)
        else:
            self._set_status("未登录")

    def _on_session_event(self, kind: str, message: str):
        """登录成功后的会话变化：浏览器被关 / U盾 被拔 / U盾 恢复。"""
        if kind == "browser_closed":
            self._logged_in = False
            self._log(f"[监控] {message}")
            self._set_status(message)
        elif kind == "ukey_removed":
            self._logged_in = False
            self._log(f"[监控] {message}")
            self._set_status(UKEY_REMOVED_MESSAGE)
        elif kind == "ukey_back":
            self._log(f"[监控] {message}")
            if not self._logged_in:
                self._set_status("未登录")

    def _on_exit(self):
        worker = self._login_worker
        if worker is not None:
            if worker.isRunning():
                if worker.monitoring:
                    self._log("[提示] 正在结束会话监控…")
                else:
                    self._log("[提示] 正在中断登录…")
                worker.shutdown()
                worker.wait(5000)
            worker.close_browser()  # Edge 是用户自己的浏览器，只断开监控、不关它

        sync_worker = self._sync_worker
        if sync_worker is not None and sync_worker.isRunning():
            self._log("[提示] 正在结束同步任务…")
            sync_worker.wait(3000)
        self.window.close()

    # ---------- 同步（按界面勾选项抓数 -> 本地库 -> 推送） ----------
    def _selected_kinds(self) -> list[str]:
        """按界面勾选返回要同步的类型（票面两类与背书流水已在前端暂时隐藏）。"""
        ui = self.pages[0]
        kinds: list[str] = []
        if ui.main_chk_fund_flow.isChecked():
            kinds.append("资金流水")
        if ui.main_chk_discount_flow.isChecked():
            kinds.append("贴现流水")
        if ui.main_chk_endorse_flow.isChecked():
            kinds.append("背书流水")
        return kinds

    def _on_sync(self):
        # 已在跑的同步不让重复启动
        if self._sync_worker is not None and self._sync_worker.isRunning():
            self._log("[提示] 同步正在进行中，请稍候…")
            return

        ui = self.pages[0]
        kinds = self._selected_kinds()
        begin = ui.main_input_date_start.text().strip()
        end = ui.main_input_date_end.text().strip()

        # 同步前校验（配置是否填全、是否勾选了可同步项），原因直接写日志
        blockers = SyncService().check_ready(kinds, begin, end)
        if blockers:
            for reason in blockers:
                self._log(f"[同步] 无法开始：{reason}")
            return

        self._set_status("同步中…")
        ui.main_btn_sync.setEnabled(False)
        worker = SyncWorker(kinds, begin, end)
        worker.log_line.connect(self._log)
        worker.item_done.connect(self._on_sync_item)
        worker.finished_all.connect(self._on_sync_done)
        self._sync_worker = worker
        self._log(f"[同步] 开始：{', '.join(kinds)}（{begin} ~ {end}）；已勾选项入本地库后推送飞诺")
        worker.start()

    def _on_sync_item(self, kind: str, fetched: int, written: int, ok: bool, message: str):
        if not kind:
            self._log(f"[同步] 错误：{message}")
            return
        if ok:
            self._log(f"[同步] {kind} 完成：抓取 {fetched} 条，入库 {written} 条")
        else:
            self._log(f"[同步] {kind} 失败：{message or '未知错误'}")

    def _on_sync_done(self):
        self.pages[0].main_btn_sync.setEnabled(True)
        self._set_status("同步完成")
        self._log("[同步] 全部结束；数据已入本地库 output/bank_data.db")

    @staticmethod
    def _toggle_api(ui, enabled: bool):
        for widget in (
            ui.api_input_url, ui.api_input_key, ui.api_input_bank,
            ui.api_input_tool, ui.api_input_batch, ui.api_chk_abort,
        ):
            widget.setEnabled(enabled)

    # ---------- 启动 ----------
    def run(self) -> int:
        self.window.show()
        return self.app.exec()


def main():
    return ChangjiangApp().run()


if __name__ == "__main__":
    sys.exit(main())
