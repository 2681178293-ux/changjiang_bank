# -*- coding: utf-8 -*-
"""U盾（USBKey）识别封装。

判定依据（按可靠度排序，任一成立即认为"U盾在位"）：

1. **CSP 密钥容器枚举**（最可靠）：
   `certutil -csp "<加密服务提供程序>" -key`
   这个命令必须真的和 U盾 里的芯片通信才能列出密钥容器。
   CSP 名不写死——直接从证书库里的「提供程序」字段读（换银行、换批次 U盾 都不用改代码），
   读不到时才用 config/.env 的 `UKEY_CSP_NAMES` 兜底。
   U盾 没插时该命令会返回 `NTE_BAD_PROVIDER / 找不到密钥容器`；
   插好后能列出形如 `02DD565BB466A5C67 [默认容器]` 的条目。
   这里拿证书里的容器名去比对，能同时确认"是本行 U盾"且"就是这张证书的 U盾"。

2. **智能卡读卡器 ATR**（pyscard）：
   需要 Windows Smart Card 服务（SCardSvr）在运行；部分 U盾 走厂商 CSP 直连、
   不注册读卡器，因此这条只作为加分项，不作为唯一依据。

3. **证书库**（`certutil -store -user My`）：
   只用来判断"证书是否已安装""是否为本行证书"。
   ⚠️ 注意：该命令输出的 `错误: 容器名称不一致` / `无法针对私钥验证证书公钥`
   在 U盾 **已插入**时同样会出现（CSP 的已知行为），**不能**用来判断令牌是否在位——
   这是早期版本误报"未插入"的根因，已弃用。

三者组合即可区分：没装证书 / 装了但没插 / 已插入可用。
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from config.settings import SETTINGS, Settings

# 证书块分隔行：================ 证书 0 ================
_BLOCK_RE = re.compile(r"^=+\s*证书\s*(\d+)\s*=+\s*$")
# 容器行：`  02DD565BB466A5C67 [默认容器]`
_CONTAINER_RE = re.compile(r"^\s{2,}([0-9A-Za-z_.\-\\]+)\s*(\[[^\]]*\])?\s*$")
# list_csp_containers 超时的专用标记（区别于"确实读不到容器"）
_TIMEOUT_ERROR = "__certutil_timeout__"
# 后台调用 certutil 时不弹出控制台窗口。
# --windowed 打包后本程序没有控制台，每次调用控制台子进程都会闪一个 cmd 窗口；
# 会话监控每 3 秒检查一次 U盾，闪烁就会反复出现。
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _decode(raw: bytes) -> str:
    """certutil 在中文 Windows 上输出 ANSI(cp936)，这里做多编码兜底。"""
    for enc in ("mbcs", "gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _friendly_csp_error(text: str) -> str:
    """把 certutil/CSP 的错误码翻译成用户能看懂的话。"""
    low = text.lower()
    if "0x80090013" in low or "nte_bad_provider" in low or "无效的提供程序" in text:
        return "系统读不到 U盾 的密钥容器，请重新插拔 U盾（建议插主机后置 USB 口）"
    if "0x8010001d" in low:
        return "Windows 智能卡服务（SCardSvr）未运行，请在服务里启动后重试"
    if "0x8009000d" in low or "nte_no_key" in low:
        return "U盾里没有可用的证书容器，请确认插入的是正确的 U盾"
    return text[:120]


@dataclass
class CertificateInfo:
    """证书库中的一张证书。"""

    index: int
    serial: str = ""
    subject: str = ""
    issuer: str = ""
    sha1: str = ""
    csp: str = ""
    container: str = ""
    not_after: str = ""
    errors: list[str] = field(default_factory=list)

    def matches(self, keywords: tuple[str, ...]) -> bool:
        haystack = " ".join((self.subject, self.issuer, self.csp, self.container)).lower()
        return any(kw.lower() in haystack for kw in keywords)

    def summary(self) -> str:
        return f"{self.subject}（截止 {self.not_after or '未知'}）"


@dataclass
class UKeyInfo:
    """一次识别结果。"""

    present: bool = False
    is_target_bank: bool = False
    certificate: CertificateInfo | None = None
    atr: str = ""
    containers: list[str] = field(default_factory=list)
    csp: str = ""
    evidences: list[str] = field(default_factory=list)
    hint: str = ""

    def describe(self) -> str:
        return "；".join(self.evidences) if self.evidences else self.hint


class UKeyReader:
    """识别江苏长江商业银行 U盾是否已插入。"""

    # 证书库读取较慢（certutil 约 2 秒），轮询检测时做短暂缓存
    CERT_CACHE_SECONDS = 15.0

    def __init__(self, settings: Settings = SETTINGS) -> None:
        self._s = settings
        self._cert_cache: list[CertificateInfo] = []
        self._cert_cache_time = 0.0

    # ---------- 证据 1：读卡器 / ATR ----------
    @staticmethod
    def _read_atr() -> tuple[str, str]:
        """返回 (ATR, 错误说明)。pyscard 不可用或服务未启动时 ATR 为空。"""
        try:
            from smartcard.System import readers
            from smartcard.util import toHexString
        except Exception as exc:  # 未安装 pyscard
            return "", f"pyscard 不可用：{exc}"

        try:
            reader_list = readers()
        except Exception as exc:
            return "", f"无法访问智能卡服务：{exc}"

        if not reader_list:
            return "", "未发现智能卡读卡器"

        last = "未知原因"
        for reader in reader_list:
            try:
                conn = reader.createConnection()
                conn.connect()
                atr = toHexString(conn.getATR())
                conn.disconnect()
                return atr, ""
            except Exception as exc:
                last = str(exc)
        return "", f"读卡器无卡片：{last}"

    # ---------- 证据 2：证书库 ----------
    def list_certificates(self, use_cache: bool = True) -> list[CertificateInfo]:
        """解析 `certutil -store -user My`，返回个人证书库里的所有证书。"""
        now = time.time()
        if use_cache and self._cert_cache and now - self._cert_cache_time < self.CERT_CACHE_SECONDS:
            return self._cert_cache

        try:
            proc = subprocess.run(
                ["certutil", "-store", "-user", "My"],
                capture_output=True,
                timeout=20,
                creationflags=_NO_WINDOW_FLAGS,
            )
        except Exception:
            return []

        text = _decode(proc.stdout or b"")
        certs: list[CertificateInfo] = []
        current: CertificateInfo | None = None
        in_store = False

        for raw_line in text.splitlines():
            line = raw_line.strip()
            match = _BLOCK_RE.match(line)
            if match:
                current = CertificateInfo(index=int(match.group(1)))
                certs.append(current)
                in_store = True
                continue
            if current is None or not line:
                continue

            if line.startswith("序列号:"):
                current.serial = line.split(":", 1)[1].strip()
            elif line.startswith("颁发者:"):
                current.issuer = line.split(":", 1)[1].strip()
            elif line.startswith("使用者:"):
                current.subject = line.split(":", 1)[1].strip()
            elif line.startswith("证书哈希"):
                current.sha1 = line.split(":", 1)[1].strip()
            elif line.startswith("NotAfter:"):
                current.not_after = line.split(":", 1)[1].strip()
            elif line.startswith("提供程序"):
                current.csp = line.split("=", 1)[-1].strip()
            elif line.startswith("简单容器名称") and not current.container:
                current.container = line.split(":", 1)[-1].strip()
            elif line.startswith("错误"):
                current.errors.append(line)

        if not in_store:
            return []

        self._cert_cache = certs
        self._cert_cache_time = now
        return certs

    # ---------- 证据 3：CSP 密钥容器枚举 ----------
    @staticmethod
    def list_csp_containers(csp: str, timeout: float = 25) -> tuple[list[str], str]:
        """调用 `certutil -csp <csp> -key`，返回 (容器名列表, 错误说明)。

        这条命令必须与 U盾 芯片通信才能返回容器，因此是"令牌在位"的强证据。
        """
        try:
            proc = subprocess.run(
                ["certutil", "-csp", csp, "-key"],
                capture_output=True,
                timeout=timeout,
                creationflags=_NO_WINDOW_FLAGS,
            )
        except subprocess.TimeoutExpired:
            return [], _TIMEOUT_ERROR
        except Exception as exc:
            return [], f"执行 certutil 失败：{exc}"

        text = _decode((proc.stdout or b"") + (proc.stderr or b""))
        if proc.returncode != 0:
            first = next(
                (l.strip() for l in text.splitlines() if "CertUtil" in l and "失败" in l),
                "",
            )
            return [], first or f"certutil 返回码 {proc.returncode}"

        containers: list[str] = []
        for raw_line in text.splitlines():
            # 只认缩进两格、形如容器的行；"RSA" / "AT_KEYEXCHANGE" 之类会被过滤掉
            match = _CONTAINER_RE.match(raw_line.rstrip())
            if not match:
                continue
            name = match.group(1).strip()
            if name.upper() in ("RSA", "AT_KEYEXCHANGE", "AT_SIGNATURE", "ECDSA"):
                continue
            if name in containers:
                continue
            containers.append(name)
        return containers, ""

    def _csp_candidates(self, targets: list[CertificateInfo]) -> list[str]:
        """候选 CSP：先用证书里带出来的，再补配置里兜底的。"""
        names: list[str] = []
        for cert in targets:
            if cert.csp and cert.csp not in names:
                names.append(cert.csp)
        for name in self._s.ukey_csp_names:
            if name and name not in names and any(
                kw.lower() in name.lower() for kw in self._s.ukey_keywords
            ):
                names.append(name)
        return names

    # ---------- 组合判断 ----------
    def quick_present(self, csp: str = "") -> bool | None:
        """快速判断 U盾 是否仍在位（登录成功后反复调用，不能太慢）。

        只做一次 CSP 密钥容器枚举，不读证书库：
            拔掉 U盾 后 `certutil -csp ... -key` 拿不到容器 → 返回 False
        返回 None 表示"名都没有，判断不了"（例如还没识别过证书）。
        """
        names: list[str] = []
        if csp:
            names.append(csp)
        targets = [c for c in self._cert_cache if c.matches(self._s.ukey_keywords)]
        for name in self._csp_candidates(targets):
            if name not in names:
                names.append(name)
        if not names:
            return None
        timed_out = False
        for name in names:
            containers, error = self.list_csp_containers(name, timeout=8)
            if containers:
                return True
            if error == _TIMEOUT_ERROR:
                timed_out = True
        # 驱动卡住导致枚举超时时不下结论，避免误报"U盾 已拔出"
        return None if timed_out else False

    def detect(self) -> UKeyInfo:
        certs = self.list_certificates()
        targets = [c for c in certs if c.matches(self._s.ukey_keywords)]

        info = UKeyInfo()
        if targets:
            info.is_target_bank = True
            info.certificate = targets[0]
            info.evidences.append(f"证书库找到{self._s.bank_name}证书：{targets[0].summary()}")

        # 证据 1：CSP 容器枚举
        csp_error = ""
        for csp in self._csp_candidates(targets):
            containers, error = self.list_csp_containers(csp)
            if containers:
                info.csp = csp
                info.containers = containers
                break
            csp_error = csp_error or error

        if info.containers:
            names = "、".join(info.containers[:3])
            info.evidences.append(f"U盾密钥容器可读取（{names}），判定 U盾已插入")
            info.present = True
            if not targets:
                # 令牌在、密钥在，但证书没注册到 Windows 证书库——网银页可能选不到证书
                info.evidences.append(
                    "但 Windows 证书库还没有本行证书：请打开「江苏长江商业银行USBKey管理工具」"
                    "把证书注册/安装到系统，否则网银页选不到证书"
                )
            if targets and targets[0].container:
                if targets[0].container in info.containers:
                    info.evidences.append(f"容器与证书匹配：{targets[0].container}")
                else:
                    info.evidences.append(
                        f"注意：证书容器 {targets[0].container} 不在 U盾 容器列表中，"
                        f"可能插的不是这张证书对应的 U盾"
                    )

        # 证据 2：读卡器 ATR
        if not info.present:
            atr, atr_error = self._read_atr()
            if atr:
                info.atr = atr
                info.present = True
                info.evidences.append(f"读卡器已读到卡片（ATR={atr}）")
            elif atr_error:
                csp_error = csp_error or atr_error

        if not info.present:
            if not certs:
                info.hint = (
                    f"未在证书库找到任何证书：请先安装{self._s.bank_name}企业证书管理软件与 U盾驱动"
                )
            elif not targets:
                info.hint = (
                    f"证书库有证书，但没有{self._s.bank_name}证书："
                    f"请确认插入的是本行 U盾（识别关键字 {','.join(self._s.ukey_keywords)}）"
                )
            else:
                info.hint = f"已找到{self._s.bank_name}证书，但 U盾 未响应（未插入、没插好或接口供电不足）"
                if csp_error:
                    info.hint += f"；{_friendly_csp_error(csp_error)}"
        return info

    def wait_for_ukey(
        self,
        timeout: int | None = None,
        poll: float | None = None,
        on_tick: Callable[[UKeyInfo, int], None] | None = None,
    ) -> UKeyInfo:
        """在 timeout 秒内反复检测，直到 U盾插入；超时返回最后一次结果。"""
        timeout = self._s.ukey_wait_seconds if timeout is None else timeout
        poll = self._s.ukey_poll_seconds if poll is None else poll
        deadline = time.time() + max(0, timeout)
        last = self.detect()
        attempt = 1
        if on_tick:
            on_tick(last, timeout)
        while not last.present and time.time() < deadline:
            time.sleep(poll)
            attempt += 1
            last = self.detect()
            if on_tick and (last.present or attempt % 3 == 0):
                on_tick(last, max(0, int(deadline - time.time())))
        return last
