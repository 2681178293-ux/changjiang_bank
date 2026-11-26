# -*- coding: utf-8 -*-
"""一次性探针：发一条带测试标记的背书流水到飞诺 bank-endorse/import。

参照老师项目 abc_ebill_query/core/feinuo_api.py 的 map_bank_endorse_row 字段映射
和 FeinuoApiClient.import_batch 请求格式。只发一条，用于确认接口接受。
数据带 PROBE-TEST 标记，验证后可在飞诺后台按流水号删除。
"""
import json
import os

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "erp_config.json"), encoding="utf-8") as fh:
    cfg = json.load(fh)

base = str(cfg["baseUrl"]).rstrip("/")
key = cfg["apiKey"]
try:
    group = int(cfg.get("groupName") or 4)
except (TypeError, ValueError):
    group = 4

# 一条探针：字段全部带 TEST 标记，方便后台识别删除
item = {
    "billNumber": "PROBE-TEST-DO-NOT-USE-ENDORSE-001",
    "subBillRange": "000000000001",
    "faceAmount": 0.01,
    "endorser": "探针测试-转出方-可删",
    "endorsee": "探针测试-转入方-可删",
    "ebankCompany": cfg.get("ownerCompany", "南京卓益安金属材料有限公司"),
    "ebankAccount": "80200240000100892434",
    "bankName": "长江银行",
    "businessDate": "2026-11-25",
    "transactionTime": "2026-11-25 14:30:00",
    "transactionSerial": "PROBE-TEST-ENDORSE-SERIAL-001",
    "busiTypeName": "背书转让结果",
    "groupName": group,
    "sourceFile": cfg.get("sourceFile", "长江网银工具"),
}

body = {"updateSupport": True, "items": [item], "groupName": group}
url = base + "/feinuo-api/bank-endorse/import"

print("POST", url)
print("BODY", json.dumps(body, ensure_ascii=False))
resp = requests.post(
    url,
    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    headers={
        "Content-Type": "application/json; charset=utf-8",
        "X-Api-Key": key,
        "Connection": "close",
    },
    timeout=60,
    verify=False,
)
print("HTTP", resp.status_code)
print("RESP", resp.text[:2000])
