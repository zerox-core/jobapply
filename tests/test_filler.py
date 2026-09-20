# -*- coding: utf-8 -*-
"""字段映射与校验单元测试（假 LLM，不打真实 API）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobapply.filler import map_fields, validate_fills, ATTACH_RESUME

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if detail and not cond else ""))


FIELDS = [
    {"idx": 0, "tag": "input", "type": "text", "label": "姓名", "name": "name", "value": ""},
    {"idx": 1, "tag": "select", "type": "select", "label": "最高学历", "name": "degree", "value": "",
     "options": [{"value": "", "text": "请选择"}, {"value": "bk", "text": "本科"}, {"value": "ss", "text": "硕士研究生"}]},
    {"idx": 2, "tag": "input", "type": "radio", "label": "男", "name": "gender", "value": "male"},
    {"idx": 3, "tag": "input", "type": "radio", "label": "女", "name": "gender", "value": "female"},
    {"idx": 4, "tag": "input", "type": "checkbox", "label": "接受调剂", "name": "adjust", "value": ""},
    {"idx": 5, "tag": "input", "type": "file", "label": "上传简历", "name": "resume", "value": ""},
    {"idx": 6, "tag": "textarea", "type": "textarea", "label": "自我评价", "name": "eval", "value": ""},
]

LLM_RETURN = {"fills": [
    {"idx": 0, "value": "张三", "confidence": 0.99, "reason": "profile.basic.name"},
    {"idx": 1, "value": "本科", "confidence": 0.95, "reason": "education"},
    {"idx": 1, "value": "不存在的学历", "confidence": 0.5, "reason": "非法选项应被丢弃"},
    {"idx": 2, "value": True, "confidence": 0.9, "reason": "gender=男"},
    {"idx": 3, "value": True, "confidence": 0.9, "reason": "同组第二个 true 应被丢弃"},
    {"idx": 4, "value": "true", "confidence": 0.8, "reason": "checkbox 字符串 true 也可"},
    {"idx": 5, "value": ATTACH_RESUME, "confidence": 0.9, "reason": "简历上传字段"},
    {"idx": 5, "value": "随便什么", "confidence": 0.3, "reason": "file 非 ATTACH_RESUME 应丢弃"},
    {"idx": 6, "value": "  ", "confidence": 0.4, "reason": "纯空白文本应丢弃"},
    {"idx": 99, "value": "幽灵字段", "confidence": 0.9, "reason": "不存在的 idx 应丢弃"},
]}


class FakeChain:
    last_used = {"provider": "fake", "model": "fake"}

    def chat(self, messages, temperature=0.2, response_json=True):
        return json.dumps(LLM_RETURN, ensure_ascii=False)


fills = map_fields(FIELDS, {"basic": {"name": "张三"}}, {"company": "演示"}, FakeChain())
by_idx = {}
for f in fills:
    by_idx.setdefault(f["idx"], []).append(f)

check("文本字段保留", by_idx.get(0, [{}])[0].get("value") == "张三")
check("select 合法选项保留", any(f["value"] == "本科" for f in by_idx.get(1, [])))
check("select 非法选项丢弃", not any(f["value"] == "不存在的学历" for f in by_idx.get(1, [])))
check("radio 组只保留一个 true",
      sum(1 for i in (2, 3) for f in by_idx.get(i, []) if f["value"] is True) == 1,
      by_idx.get(2), )
check("checkbox 字符串 true 归一为 bool", by_idx.get(4, [{}])[0].get("value") is True)
check("file ATTACH_RESUME 保留", any(f["value"] == ATTACH_RESUME for f in by_idx.get(5, [])))
check("file 非标记值丢弃", not any(f["value"] == "随便什么" for f in by_idx.get(5, [])))
check("空白文本丢弃", 6 not in by_idx)
check("幽灵 idx 丢弃", 99 not in by_idx)
check("每条都带 confidence/reason", all("confidence" in f and "reason" in f for f in fills))

failed = [r for r in RESULTS if not r[1]]
print(f"\n== test_filler: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ==")
sys.exit(1 if failed else 0)
