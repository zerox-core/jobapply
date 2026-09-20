"""jobapply 内置对话助手：WebUI 聊天面板的 LLM 后端。

用户在 WebUI 里直接和它对话：它不确定的就问，用户的回答经 actions 写入知识库。
模型走 config.yaml 里配置的 LLMChain，不依赖任何外部服务。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from jobapply import kb as kb_mod
from jobapply.llm import parse_json

BASE = Path(__file__).resolve().parent.parent
HISTORY_PATH = BASE / "data" / "chat_history.json"

_SYS = """你是 jobapply 秋招网申自动化系统的内置助手，运行在用户自己的 WebUI 对话面板里。

你的职责：
1. 回答用户关于网申填写、简历、职位的问题；
2. 用户在对话里给出的任何个人信息或决定（GPA、证明人、期望城市等），用 set_fact 动作写入知识库——写入后系统以后填表会自动使用；
3. 知识库「待补缺口」里的字段，主动向用户询问；用户给了答案用 resolve_gap 动作；
4. 用户让你操作浏览器时，用 open_url / extract / fill 动作。

只输出 JSON（不要输出任何别的内容）：
{"reply": "给用户看的回复，口语化简体中文，简短直白", "actions": [动作...]}

动作类型：
- {"op": "set_fact", "key": "事实键", "value": "值"}  存一条知识库事实。事实键参考已有键命名（如 basic.gpa、expected.city、expected.salary）
- {"op": "resolve_gap", "field": "缺口字段名", "value": "用户给的值"}  用户补充了缺口（value 可省略=仅摘除）
- {"op": "open_url", "url": "https://..."}  打开浏览器到某页面
- {"op": "extract"}  读取当前页面表单并 AI 预填
- {"op": "fill"}  把预填值写入页面（只在用户明确说写入/填充时用；系统绝不自动提交表单）
没有动作时 actions 给 []。

铁律：绝不编造用户的个人信息，不确定就直接问；reply 别写客套话，一次回复尽量不超过三句。"""


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def history(limit=60):
    if HISTORY_PATH.exists():
        try:
            h = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(h, list):
                return h[-limit:]
        except Exception:
            pass
    return []


def _save(h):
    HISTORY_PATH.write_text(json.dumps(h[-100:], ensure_ascii=False, indent=2), encoding="utf-8")


def append(role, content, actions=None):
    h = history(100)
    item = {"role": role, "content": content, "ts": _now()}
    if actions:
        item["actions"] = actions
    h.append(item)
    _save(h)


def patch_last_actions(actions):
    """把最后一条 assistant 消息的 actions 替换为执行结果明细（供历史回放显示）。"""
    h = history(100)
    for m in reversed(h):
        if m.get("role") == "assistant":
            m["actions"] = actions
            break
    _save(h)


def _kb_context():
    kb = kb_mod.load()
    lines = []
    if kb["facts"]:
        lines.append("知识库已有事实：")
        for k, f in kb["facts"].items():
            v = (f.get("value") or "").strip()
            st = f.get("status", "")
            lines.append("- %s = %s [%s]" % (k, v if v else ("（空，" + st + "）"), st))
    if kb["gaps"]:
        lines.append("待补缺口（合适时机主动问用户）：")
        for g in kb["gaps"]:
            plat = g.get("platform") or ""
            lines.append("- %s%s" % (g["field"], ("（" + plat + "）") if plat else ""))
    return "\n".join(lines) if lines else "（知识库为空）"


def _profile_context(profile):
    if not profile:
        return "（尚未上传简历）"
    b = profile.get("basic") or {}
    edu = (profile.get("education") or [{}])
    edu = edu[0] if edu else {}
    exp = profile.get("expected") or {}
    parts = ["姓名 " + str(b.get("name") or ""),
             "学校 " + str(edu.get("school") or "") + " " + str(edu.get("major") or "")
             + " " + str(edu.get("degree") or ""),
             "期望 " + str(exp.get("position") or "") + " " + str(exp.get("city") or "")]
    return "简历要点：" + "；".join(p for p in parts if p.split(" ", 1)[1].strip())


def handle_message(text, chain, profile=None):
    """处理一条用户消息。返回 {"reply": str, "actions": [...]}。

    actions 只解析不执行（执行方是 server，掌握浏览器会话）；解析失败时整段当回复。
    """
    messages = [{"role": "system",
                 "content": _SYS + "\n\n" + _kb_context() + "\n\n" + _profile_context(profile)}]
    for m in history(16):
        messages.append({"role": "user" if m.get("role") == "user" else "assistant",
                         "content": m.get("content", "")})
    messages.append({"role": "user", "content": text})
    raw = chain.chat(messages, temperature=0.3, response_json=True)
    try:
        d = parse_json(raw)
        reply = str(d.get("reply") or "").strip() or "（这条我没组织出回复，换个说法再问我一次）"
        actions = d.get("actions") or []
        if not isinstance(actions, list):
            actions = []
    except Exception:
        reply, actions = raw.strip(), []
    append("user", text)
    append("assistant", reply, actions=[a for a in actions if isinstance(a, dict)])
    return {"reply": reply, "actions": actions}
