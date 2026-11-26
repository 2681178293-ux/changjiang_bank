# -*- coding: utf-8 -*-
"""飞诺推送配置读写：项目根 erp_config.json。

分组以主界面下拉框为准：A=1 B=2 C=3 D=4 E=5。
页面选择后即时写回 groupName，推送时从这里读当前分组。
"""
from __future__ import annotations

import json
from pathlib import Path

from config.settings import BASE_DIR

CONFIG_PATH = Path(BASE_DIR) / "erp_config.json"

GROUP_LETTERS = {1: "A", 2: "B", 3: "C", 4: "D", 5: "E"}
LETTER_GROUPS = {v: k for k, v in GROUP_LETTERS.items()}


def _load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    raw = CONFIG_PATH.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> bool:
    try:
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return True
    except OSError:
        return False


def load_config() -> dict:
    return _load()


def get_group() -> int | None:
    """当前分组数字（1-5）；无效返回 None。"""
    try:
        n = int(_load().get("groupName"))
    except (TypeError, ValueError):
        return None
    return n if n in GROUP_LETTERS else None


def set_group(group: int) -> bool:
    if group not in GROUP_LETTERS:
        return False
    data = _load()
    data["groupName"] = group
    return _save(data)


def set_owner_company(company: str) -> bool:
    """登录时从网银账号下拉框识别到的本方公司名，写回 ownerCompany。"""
    company = (company or "").strip()
    if not company:
        return False
    data = _load()
    data["ownerCompany"] = company
    return _save(data)


def group_label(group: int | None) -> str:
    return GROUP_LETTERS.get(group, "?") if group else "?"
