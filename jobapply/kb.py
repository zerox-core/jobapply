"""个人资料知识库：会话沉淀的可调用事实、平台规则、待补缺口。

设计意图（2026-09-20 用户拍板）：
- 边干边沉淀：会话中得到的答案 / 平台必填发现 / 用户拍板的映射都入库，后续直接调用；
- 填充回退链：简历 profile 里没有的 → 查本知识库补充；
- 仍填不了的 → 记入 gaps，等用户补充（补充后自动变成事实）。

data/profile_kb.json 结构：
  facts:           {key: {value, status, source, note, ts}}
                   status: user_confirmed / inferred / user_skipped（空值时记缺口）
  platform_rules:  {domain: {required_fields, option_map, mastery_default, edit_button, save_button, notes}}
  gaps:            [{field, platform, context, ts}]   # (field, platform) 去重
  history:         [{ts, op, ...}]
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent.parent
KB_PATH = BASE / "data" / "profile_kb.json"

# 标签关键词 → 事实键（命中即尝试用知识库回填；无值则记缺口）
LABEL_MAP = [
    (("gpa", "成绩", "绩点", "平均分"), "basic.gpa"),
    (("专业类别", "专业分类", "学科类别", "专业门类"), "education.major_category"),
    (("掌握程度", "熟练程度", "技能水平", "掌握水平"), "skills.level_default"),
    (("证明人电话", "联系人电话", "推荐人电话"), "internship.reference_phone"),
    (("证明人职务", "联系人职务"), "internship.reference_role"),
    (("证明人关系", "联系人关系"), "internship.reference_relation"),
    (("证明人", "联系人姓名", "推荐人"), "internship.reference_name"),
    (("政治面貌",), "basic.political"),
    (("户籍", "户口"), "basic.hukou"),
    (("性别",), "basic.gender"),
    (("出生", "生日", "birth"), "basic.birth"),
    (("期望城市", "工作城市", "意向城市"), "expected.city"),
    (("期望薪资", "薪资"), "expected.salary"),
    (("github", "个人主页", "个人链接", "博客"), "basic.links"),
]

_EMPTY = {"facts": {}, "platform_rules": {}, "gaps": [], "history": []}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load():
    if KB_PATH.exists():
        try:
            kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
            for k, v in _EMPTY.items():
                kb.setdefault(k, v.copy() if isinstance(v, (dict, list)) else v)
            return kb
        except Exception:
            pass
    return json.loads(json.dumps(_EMPTY))


def save(kb):
    KB_PATH.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")


def set_fact(key, value, source="manual", status="user_confirmed", note=""):
    kb = load()
    kb["facts"][key] = {"value": value, "status": status,
                        "source": source, "note": note, "ts": _now()}
    kb["history"].append({"ts": _now(), "op": "set_fact", "key": key,
                          "value": value, "source": source})
    kb["history"] = kb["history"][-200:]
    save(kb)


def get_fact(key):
    return load()["facts"].get(key)


def set_platform_rule(domain, source="manual", **rules):
    kb = load()
    cur = kb["platform_rules"].get(domain, {})
    cur.update(rules)
    cur["ts"] = _now()
    kb["platform_rules"][domain] = cur
    kb["history"].append({"ts": _now(), "op": "set_rule", "domain": domain, "source": source})
    save(kb)


def platform_rule(domain):
    return load()["platform_rules"].get(domain, {})


def add_gap(field, platform, context):
    kb = load()
    field = (field or "").strip()
    if not field:
        return
    for g in kb["gaps"]:
        if g["field"] == field and g.get("platform", "") == platform:
            return  # 已记录
    kb["gaps"].append({"field": field, "platform": platform,
                       "context": context, "ts": _now()})
    save(kb)


def resolve_gap(field, platform="", value="", source="manual"):
    """用户补了缺口：从 gaps 摘除；给了 value 就同时存为事实（下次直接调用）。"""
    kb = load()
    field = (field or "").strip()
    kb["gaps"] = [g for g in kb["gaps"]
                  if not (g["field"] == field and (not platform or g.get("platform", "") == platform))]
    save(kb)
    if value:
        key = match_key(field) or ("manual." + field)
        set_fact(key, value, source=source, status="user_confirmed",
                 note="由缺口补充：" + field)
        return key
    return None


def match_key(text):
    t = (text or "").lower()
    for kws, key in LABEL_MAP:
        if any(kw.lower() in t for kw in kws):
            return key
    return None


def _domain_of(page_url):
    try:
        return urlparse(page_url or "").netloc
    except Exception:
        return ""


def backfill_fills(fills, fields, page_url=""):
    """LLM 填完后，用知识库回退补充。返回 (fills, gaps, kb_added_count)。

    - LLM 已覆盖 / 平台已带值 / file 类 → 不动；
    - 标签命中 LABEL_MAP 且知识库有值 → 补填（select 需值在选项内，支持 option_map 映射）；
    - 标签命中但知识库无值 → 记缺口 gaps。
    """
    kb = load()
    domain = _domain_of(page_url)
    rules = kb["platform_rules"].get(domain, {})
    filled_idx = {f.get("idx") for f in fills}
    added = 0
    new_gaps = []
    for f in fields:
        idx = f.get("idx")
        if idx in filled_idx:
            continue
        typ = (f.get("type") or "").lower()
        if typ in ("file", "submit", "button", "hidden", "image", "reset", "password"):
            continue
        if (f.get("value") or "").strip():
            continue  # 平台已带出值
        label = " ".join([(f.get("label") or ""), (f.get("name") or ""),
                          (f.get("placeholder") or "")]).strip()
        key = match_key(label)
        if not key:
            continue
        fact = kb["facts"].get(key) or {}
        val = (fact.get("value") or "").strip()
        disp = (f.get("label") or f.get("name") or ("字段" + str(idx))).strip()
        if not val:
            add_gap(disp, domain, "知识库无值（" + key + "）")
            new_gaps.append({"field": disp, "key": key})
            continue
        if f.get("tag") == "select":
            opts = {o.get("text") for o in (f.get("options") or [])}
            if val not in opts:
                mapped = (rules.get("option_map") or {}).get(val)
                if not mapped and rules.get("mastery_default") in opts:
                    mapped = rules["mastery_default"]
                if mapped and mapped in opts:
                    val = mapped
                else:
                    add_gap(disp, domain, "知识库值「" + val + "」不在选项内")
                    new_gaps.append({"field": disp, "key": key})
                    continue
        fills.append({"idx": idx, "value": val, "confidence": 0.8,
                      "reason": "知识库:" + key, "source": "kb"})
        added += 1
    return fills, new_gaps, added
