"""字段映射（LLM）与填写值校验。"""
from __future__ import annotations

import json

from .llm import chat_json

ATTACH_RESUME = "ATTACH_RESUME"

MAP_PROMPT = """你是网申表单填写助手。根据候选人简历 profile 和岗位信息，为页面表单字段逐一给出填写值。

输出 JSON：{"fills": [{"idx": 数字, "value": 值, "confidence": 0到1, "reason": "一句话来源说明"}]}
规则：
- idx 必须来自字段清单；拿不准的字段不要输出（宁可不填，也不能瞎编）。
- text/textarea/date/month/number/tel/email：value 为字符串；date 用 YYYY-MM-DD，month 用 YYYY-MM。
- select：value 必须是该字段 options 中某一项的 text，原样照抄，不要自造选项。
- radio/checkbox：value 用 true/false；同名 radio 组最多一个 true。
- file：字段为简历/附件上传时 value 填 "ATTACH_RESUME"，其余 file 字段跳过。
- 长文本（实习/项目/自我评价）结合岗位方向组织，150 字以内，只能基于 profile 里的事实，不编造。
- 字段已有值且合理时可以跳过不填。
"""

_KEEP_KEYS = ("idx", "tag", "type", "label", "placeholder", "required",
              "options", "value", "name")


def map_fields(fields, profile, job, chain) -> list:
    brief = [{k: f[k] for k in _KEEP_KEYS if k in f} for f in fields]
    messages = [
        {"role": "system", "content": MAP_PROMPT},
        {"role": "user", "content":
            "【岗位】\n" + json.dumps(job, ensure_ascii=False) +
            "\n【简历 profile】\n" + json.dumps(profile, ensure_ascii=False) +
            "\n【页面字段】\n" + json.dumps(brief, ensure_ascii=False)},
    ]
    data = chat_json(chain, messages)
    fills = data.get("fills") if isinstance(data, dict) else data
    return validate_fills(fills or [], fields)


def validate_fills(fills, fields) -> list:
    """只保留合法填写项：idx 必须存在、select 值必须在选项内、radio 组单选、文本非空。"""
    by_idx = {f["idx"]: f for f in fields}
    out = []
    seen_radio = set()
    for item in fills:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("idx"))
        except (TypeError, ValueError):
            continue
        f = by_idx.get(idx)
        if f is None:
            continue
        val = item.get("value")
        typ = (f.get("type") or "").lower()
        tag = f.get("tag")
        if tag == "select":
            opts = {o["text"] for o in (f.get("options") or [])}
            if val not in opts:
                continue
        elif typ in ("radio", "checkbox"):
            val = val in (True, "true", "1", 1, "yes")
            if typ == "radio" and val:
                gname = f.get("name") or ""
                if gname in seen_radio:
                    continue
                seen_radio.add(gname)
        elif typ == "file":
            if val != ATTACH_RESUME:
                continue
        else:
            if not isinstance(val, str) or not val.strip():
                continue
        out.append({"idx": idx, "value": val,
                    "confidence": float(item.get("confidence") or 0),
                    "reason": str(item.get("reason") or "")})
    return out
