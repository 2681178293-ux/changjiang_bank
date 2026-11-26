# -*- coding: utf-8 -*-
"""全局配置：网银地址、超时、浏览器、U盾识别关键字。

取值优先级：同目录 `.env` > 本文件默认值。
改配置时优先改 `config/.env`，不要改代码。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

# 打包成 exe 后，配置和数据库放在 exe 同级目录，方便使用者直接修改；
# 源码运行时仍保持原来的项目根目录行为。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / "config" / ".env"


def _read_env(path: Path) -> dict[str, str]:
    """.env 极简解析（KEY=VALUE，支持 # 注释），不引入第三方依赖。"""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


_ENV = _read_env(ENV_PATH)


def _str(key: str, default: str) -> str:
    return _ENV.get(key) or default


def _int(key: str, default: int) -> int:
    try:
        return int(_ENV[key])
    except (KeyError, ValueError):
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_ENV[key])
    except (KeyError, ValueError):
        return default


def _bool(key: str, default: bool) -> bool:
    value = _ENV.get(key)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "是")


def _tuple(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = _ENV.get(key)
    if not value:
        return default
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    return items or default


@dataclass(frozen=True)
class Settings:
    """全局配置。字段名与 .env 里的键一一对应（见下方注释）。"""

    # ---- 银行 / 地址 ----
    bank_name: str = _str("BANK_NAME", "江苏长江商业银行")
    home_url: str = _str("HOME_URL", "https://www.cjccb.com")
    # 企业网银登录页：首页「网银登录」悬浮菜单里的「企业网上银行」指向这里
    ebank_url: str = _str(
        "EBANK_URL",
        "https://ebank.jindiancloud.com/ebank/prelogin.do"
        "?_locale=zh_CN&BankId=313312300018&LoginType=C",
    )
    # 认定"已经在企业网银页面"的域名（用于登录成功判断，避免误把首页当成功）
    ebank_hosts: tuple[str, ...] = _tuple("EBANK_HOSTS", ("ebank.jindiancloud.com",))
    # 直接打开登录页：首页的「企业网上银行」在 hover 菜单里，点它容易被遮罩挡住，直接开更稳
    open_ebank_directly: bool = _bool("OPEN_EBANK_DIRECTLY", True)
    # 不走直接打开时，首页「企业网上银行」按钮的选择器
    corp_login_selectors: tuple[str, ...] = _tuple(
        "CORP_LOGIN_SELECTORS",
        ("a[href*='ebank/prelogin.do']", "a:has-text('企业网上银行')"),
    )
    # 登录页出现这些元素之一，就说明登录表单已经就绪（该用户输了）
    login_ready_selectors: tuple[str, ...] = _tuple(
        "LOGIN_READY_SELECTORS",
        (
            "input[name='CifNo']",      # 企业客户号
            "input[name='UserId']",     # 登录名
            "input[type='password']",   # 装了安全控件后会出现
            "#powerpass",               # 安全控件密码框
        ),
    )
    # 登录页本身就带表单时不需要程序再点「登录」（点了等于提交空表单，只会报错）
    auto_click_login: bool = _bool("AUTO_CLICK_LOGIN", False)

    # ---- U盾识别 ----
    # 证书「使用者/颁发者/加密服务提供程序」里命中任一关键字即认为是本行证书
    ukey_keywords: tuple[str, ...] = _tuple(
        "UKEY_KEYWORDS", ("CJCCB", "长江", "cjccb", "长江商业银行")
    )
    # CSP 兜底名（正常情况从证书里自动读出来；证书读不到时才用，留空即只用证书里的）
    # 本机实测：长江银行 U盾 的加密服务提供程序名（certutil -csplist 里可查）
    ukey_csp_names: tuple[str, ...] = _tuple(
        "UKEY_CSP_NAMES", ("CJCCB ET O-KEY CSP V1.0",)
    )
    ukey_wait_seconds: int = _int("UKEY_WAIT_SECONDS", 20)      # 等用户插入 U盾的秒数
    ukey_poll_seconds: float = _float("UKEY_POLL_SECONDS", 1.5)
    # 登录成功后的会话监控：多久看一次浏览器 / 多久查一次 U盾（秒）
    monitor_poll_seconds: float = _float("MONITOR_POLL_SECONDS", 2.0)
    ukey_check_seconds: float = _float("UKEY_CHECK_SECONDS", 3.0)

    # ---- 登录等待 ----
    login_wait_seconds: int = _int("LOGIN_WAIT_SECONDS", 300)   # 等用户输完密码的秒数
    login_poll_seconds: float = _float("LOGIN_POLL_SECONDS", 2.0)
    success_confirm_times: int = _int("SUCCESS_CONFIRM_TIMES", 2)  # 连续命中几次才算成功

    # ---- U盾 口令弹窗 / 证书弹窗（自动输入口令）----
    # 口令框标题白名单：命中任一前缀就直接认（各家 U盾 厂商文案不同，按日志补）
    ukey_pin_window_titles: tuple[str, ...] = _tuple(
        "UKEY_PIN_WINDOW_TITLES",
        (
            "验证U盾密码", "请输入U盾密码", "U盾密码", "U盾口令",
            "验证USB Key密码", "USB Key密码", "USBKey密码",
            "输入口令", "验证口令", "请输入PIN",
        ),
    )
    # 自适应识别时，标题必须含其中一个词才敢自动输入（安全阀）
    pin_title_keywords: tuple[str, ...] = _tuple(
        "PIN_TITLE_KEYWORDS", ("密码", "口令", "PIN", "pin", "Key", "key", "U盾", "USB")
    )
    pin_inject_interval: tuple[float, float] = (0.06, 0.18)   # 逐字符随机间隔（秒），像人工
    pin_commit_seconds: int = _int("PIN_COMMIT_SECONDS", 8)   # 回车后等弹窗关闭的时间
    pin_max_fail_per_run: int = _int("PIN_MAX_FAIL_PER_RUN", 2)  # 熔断阈值（连错会锁 U盾）
    auto_confirm_cert: bool = _bool("AUTO_CONFIRM_CERT", True)   # 自动确认「选择证书」

    # ---- 浏览器 ----
    # msedge = 本机 Microsoft Edge（默认）；chrome = 本机 Google Chrome
    browser_channel: str = _str("BROWSER_CHANNEL", "msedge")
    browser_headless: bool = _bool("BROWSER_HEADLESS", False)    # 必须可见，用户要自己输密码
    browser_debug_port: int = _int("BROWSER_DEBUG_PORT", 9222)   # 复用已开窗口用的调试端口
    browser_profile_dir: Path = field(
        default_factory=lambda: Path(_str("BROWSER_PROFILE_DIR", str(BASE_DIR / ".browser_profile")))
    )
    navigate_timeout_ms: int = _int("NAVIGATE_TIMEOUT_MS", 45000)
    click_timeout_ms: int = _int("CLICK_TIMEOUT_MS", 4000)

    # ---- 页面定位文案 ----
    # 需要程序代点的「登录」按钮（长江的登录页自带表单，默认不点，见 AUTO_CLICK_LOGIN）
    login_button_texts: tuple[str, ...] = _tuple(
        "LOGIN_BUTTON_TEXTS", ("立即登录", "登录")
    )
    # 协议/弹窗上的"同意"按钮
    agree_texts: tuple[str, ...] = _tuple("AGREE_TEXTS", ())
    policy_texts: tuple[str, ...] = _tuple("POLICY_TEXTS", ())
    # 登录成功标志：页面出现这些文案之一
    success_texts: tuple[str, ...] = _tuple(
        "SUCCESS_TEXTS",
        (
            "安全退出", "退出登录", "退出系统", "退出",
            "账户查询", "账户余额", "余额查询", "欢迎登录",
        ),
    )
    # 长江银行登录成功后 **URL 仍停在 prelogin.do**，且标签标题保持
    # 「企业网上银行|江苏长江商业银行」，两样都不变，靠标题/URL 判不出来。
    # 改为用 Windows 可访问性（UIA）只读探测页面上的主界面专属菜单文案，
    # 这些词登录页不会出现，命中任意一个即认定登录成功。
    success_page_texts: tuple[str, ...] = _tuple(
        "SUCCESS_PAGE_TEXTS",
        ("我的主页", "查询服务", "交易录入", "待审核交易", "安全退出", "账户查询"),
    )
    # 登录成功标志：URL 含这些片段（且不含 LOGIN_URL_KEYWORDS）
    success_url_keywords: tuple[str, ...] = _tuple(
        "SUCCESS_URL_KEYWORDS", ("main", "/home", "workbench", "index", "menu")
    )
    # URL 含这些片段说明还停在登录过程里
    login_url_keywords: tuple[str, ...] = _tuple(
        "LOGIN_URL_KEYWORDS", ("prelogin", "login", "logon", "certlogin")
    )
    # 出现这些文案说明登录失败，用于提前告警（不中断等待）
    error_texts: tuple[str, ...] = _tuple(
        "ERROR_TEXTS",
        (
            "密码错误", "密码不正确", "验证码错误", "验证码不正确",
            "用户不存在", "客户号不存在", "密码已锁定", "登录名或密码",
        ),
    )


SETTINGS = Settings()

