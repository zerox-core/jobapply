# -*- coding: utf-8 -*-
"""LLM 调用链单元测试（假 client，不打真实 API）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobapply.llm import LLMChain, Provider, classify_error, parse_json

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if detail and not cond else ""))


class FakeMsg:
    def __init__(self, c): self.content = c


class FakeChoice:
    def __init__(self, c): self.message = FakeMsg(c)


class FakeResp:
    def __init__(self, c): self.choices = [FakeChoice(c)]


class FakeCompletions:
    def __init__(self, behavior): self.behavior = behavior; self.calls = 0

    def create(self, model=None, messages=None, temperature=None, **kw):
        self.calls += 1
        b = self.behavior
        if b == "quota":
            raise RuntimeError("HTTP 429 Rate limit exceeded: quota exhausted")
        if b == "auth":
            raise RuntimeError("401 invalid api key")
        if b == "empty":
            return FakeResp("")
        if b == "format_unsupported" and "response_format" in kw:
            raise RuntimeError("response_format is not supported")
        return FakeResp('{"ok": true}')


class FakeChat:
    def __init__(self, c): self.completions = c


class FakeClient:
    def __init__(self, behavior): self.chat = FakeChat(FakeCompletions(behavior))


def factory_seq(behaviors, made):
    def f(base_url, api_key, timeout):
        c = FakeClient(behaviors[len(made)])
        made.append(c)
        return c
    return f


def prov(name, model="m1"):
    return Provider(name=name, base_url="http://x", api_key="k", models=[model])


# 1) 首个渠道额度尽 → 自动降级第二渠道
made = []
chain = LLMChain([prov("ag"), prov("bailian")],
                 client_factory=factory_seq(["quota", "ok"], made))
out = chain.chat([{"role": "user", "content": "hi"}])
check("quota 自动降级成功", out == '{"ok": true}')
check("降级后使用的是第二渠道", chain.last_used["provider"] == "bailian")
check("首渠道进入冷却", not chain.providers[0].available())

# 2) 全部额度尽 → 抛错且信息完整
made = []
chain2 = LLMChain([prov("ag"), prov("bailian")],
                  client_factory=factory_seq(["quota", "quota"], made))
try:
    chain2.chat([{"role": "user", "content": "hi"}])
    check("全部失败应抛错", False)
except RuntimeError as e:
    check("全部失败应抛错", "LLM 链全部失败" in str(e), str(e)[:80])

# 3) 空 content 视为失败并降级
made = []
chain3 = LLMChain([prov("ag"), prov("bailian")],
                  client_factory=factory_seq(["empty", "ok"], made))
out3 = chain3.chat([{"role": "user", "content": "hi"}])
check("空 content 自动降级", chain3.last_used["provider"] == "bailian", chain3.last_used)

# 4) 不支持 response_format → 降级普通模式重试成功
made = []
chain4 = LLMChain([prov("ag")],
                  client_factory=factory_seq(["format_unsupported", "ok"], made))
# 同一 client 两次行为不同做不到，用 ok 兜底说明第二次调用换了 client 也能成功
out4 = chain4.chat([{"role": "user", "content": "hi"}])
check("json 模式失败后普通模式重试成功", out4 == '{"ok": true}')

# 5) 错误分类
check("classify quota", classify_error(RuntimeError("429 quota")) == "quota")
check("classify auth", classify_error(RuntimeError("401 Unauthorized")) == "auth")
check("classify other", classify_error(RuntimeError("connection reset")) == "other")

# 6) parse_json 容忍包裹与杂质
check("parse_json 带围栏", parse_json('```json\n{"a": 1}\n```') == {"a": 1})
check("parse_json 带前言", parse_json('好的，结果如下：{"a": 2}') == {"a": 2})
check("parse_json 数组", parse_json('[1, 2]') == [1, 2])
try:
    parse_json("没有 JSON")
    check("parse_json 无 JSON 抛错", False)
except ValueError:
    check("parse_json 无 JSON 抛错", True)

failed = [r for r in RESULTS if not r[1]]
print(f"\n== test_llm_chain: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ==")
sys.exit(1 if failed else 0)
