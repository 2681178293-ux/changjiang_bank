# -*- coding: utf-8 -*-
"""登录凭据（企业客户号 / 登录名 / 登录密码 / U盾 口令）的本地存取。

存放位置：`config/credentials.json`（已在 .gitignore 里排除，不会进仓库）。

为什么单独一个文件而不是写进 config/.env：
    · .env 是"环境差异配置"，会被整体覆盖/手动编辑，塞密码容易误删误提交；
    · 这个文件由「设置 · 登录」页面直接读写，字段固定、格式好校验；
    · 以后要换成 Windows 凭据管理器（win32cred），只需要换掉本模块的实现。

安全说明（务实版）：
    · 明文保存是**为了 U盾 口令自动输入**这个前提，无法避免；因此文件权限沿用用户目录，
      并且日志里一律不打印明文（只打印"已配置 / 未配置"和位数）；
    · 拿到这台电脑的人 = 拿到 U盾 之外的一切，口令保护的意义只在"锁 U盾 错次"；
    · 交付/换机时删掉这个文件即可恢复成纯人工输入。

说明：企业客户号 / 登录名 / 登录密码 仍会保留在本文件里（页面登录由人工完成，
程序不再代填），但界面不再收集和展示；U盾 口令是唯一被自动使用的字段。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from config.settings import BASE_DIR

CREDENTIALS_PATH = Path(BASE_DIR) / "config" / "credentials.json"

_LOCK = threading.Lock()


@dataclass
class Credentials:
    """一次登录需要的全部输入。"""

    client_no: str = ""      # 企业客户号（页面 CifNo）
    login_name: str = ""     # 登录名（页面 UserId）
    password: str = ""       # 登录密码（页面安全控件 powerpass）
    ukey_pin: str = ""       # U盾 口令（CSP 弹窗里输的那个）

    auto_pin: bool = True             # 自动输入 U盾 口令
    auto_confirm_cert: bool = True    # 自动确认「选择证书」弹窗
    auto_fill_password: bool = True   # 自动填写网页登录密码（Interception 驱动级输入）

    # ---------- 展示 ----------
    def masked(self) -> str:
        """日志用：只暴露"有没有配"，绝不带明文。"""
        def flag(value: str) -> str:
            return f"已配置({len(value)}位)" if value else "未配置"

        return (
            f"客户号{flag(self.client_no)}、登录名{flag(self.login_name)}、"
            f"登录密码{flag(self.password)}、U盾口令{flag(self.ukey_pin)}"
        )

    def missing(self, need_pin: bool = True) -> list[str]:
        """返回缺哪些字段（给界面/日志提示用）。"""
        names = {
            "client_no": "企业客户号",
            "login_name": "登录名",
            "password": "登录密码",
            "ukey_pin": "U盾 口令",
        }
        absent = [
            label for key, label in names.items()
            if (key != "ukey_pin" or need_pin) and not (getattr(self, key) or "").strip()
        ]
        return absent


def load() -> Credentials:
    """读凭据；文件不存在/损坏就返回默认值（不抛异常，登录流程不能被它绊住）。"""
    with _LOCK:
        data: dict = {}
        try:
            if CREDENTIALS_PATH.is_file():
                data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}

        creds = Credentials()
        if isinstance(data, dict):
            for key in (
                "client_no", "login_name", "password", "ukey_pin",
                "auto_pin", "auto_confirm_cert", "auto_fill_password",
            ):
                if key in data and data[key] is not None:
                    setattr(creds, key, data[key])

        # 环境变量兜底（方便临时覆盖，不用改文件）
        creds.ukey_pin = os.environ.get("UKEY_PIN") or creds.ukey_pin
        creds.client_no = os.environ.get("EBANK_CLIENT_NO") or creds.client_no
        creds.login_name = os.environ.get("EBANK_LOGIN_NAME") or creds.login_name
        creds.password = os.environ.get("EBANK_PASSWORD") or creds.password

        for key in ("client_no", "login_name", "password", "ukey_pin"):
            setattr(creds, key, str(getattr(creds, key) or "").strip())
        return creds


def save(creds: Credentials) -> bool:
    """写回凭据文件（原子写，避免半截 JSON）。"""
    with _LOCK:
        try:
            CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CREDENTIALS_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(asdict(creds), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(CREDENTIALS_PATH)
            return True
        except Exception:
            return False


def update(**changes) -> Credentials:
    """改几个字段并落盘（界面用）。"""
    creds = load()
    for key, value in changes.items():
        if hasattr(creds, key):
            setattr(creds, key, value)
    save(creds)
    return creds
