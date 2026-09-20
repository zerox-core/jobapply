"""LLM 调用链：按 config.yaml 中 chain 顺序调用，额度/鉴权类失败自动降级到下一个。

key 不落地存储，运行时从 config.yaml 指向的 hub_config 文件读取。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Optional

QUOTA_HINTS = ("quota", "rate limit", "429", "5h", "weekly", "limit exceeded",
               "allocationquota", "freetieronly", "arrearage", "overdue",
               "insufficient", "欠费", "余额", "额度")
AUTH_HINTS = ("401", "403", "invalid api key", "unauthorized", "authentication")


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    models: list
    cooldown_until: float = 0.0

    def available(self) -> bool:
        return time.time() >= self.cooldown_until


def classify_error(err: Exception) -> str:
    """quota=额度/限流类（换渠道），auth=鉴权类（换渠道），other=其它（原样重试）。"""
    msg = str(err).lower()
    if any(h in msg for h in QUOTA_HINTS):
        return "quota"
    if any(h in msg for h in AUTH_HINTS):
        return "auth"
    return "other"


def _default_client_factory(base_url: str, api_key: str, timeout: float):
    from openai import OpenAI
    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)


class LLMChain:
    """按 chain 顺序逐个 provider/model 尝试；额度/鉴权错误冷却该 provider 自动降级。"""

    def __init__(self, providers, cooldown_seconds: int = 300, timeout: float = 90,
                 client_factory: Optional[Callable] = None):
        self.providers = providers
        self.cooldown_seconds = cooldown_seconds
        self.timeout = timeout
        self.client_factory = client_factory or _default_client_factory
        self.last_used = None  # {"provider":..., "model":...}

    @classmethod
    def from_hub(cls, hub_data: dict, chain_cfg: list, cooldown_seconds: int = 300,
                 timeout: float = 90, client_factory=None):
        by_id = {p["id"]: p for p in hub_data.get("providers", [])}
        providers = []
        for item in chain_cfg:
            src = by_id[item["provider"]]
            providers.append(Provider(
                name=src.get("name") or item["provider"],
                base_url=src["base_url"],
                api_key=src["api_key"],
                models=[item["model"]]))
        return cls(providers, cooldown_seconds=cooldown_seconds, timeout=timeout,
                   client_factory=client_factory)

    def _call_once(self, p: Provider, model: str, messages, temperature, use_json_mode):
        client = self.client_factory(p.base_url, p.api_key, self.timeout)
        kwargs = {}
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, **kwargs)
        content = resp.choices[0].message.content
        if not content or not str(content).strip():
            raise RuntimeError("empty content")
        return content

    def chat(self, messages, temperature: float = 0.2, response_json: bool = True) -> str:
        errors = []
        for p in self.providers:
            if not p.available():
                errors.append(f"{p.name}: 冷却中，跳过")
                continue
            for model in p.models:
                # 先试 json 模式；不支持 response_format 的渠道降级为普通调用重试一次
                attempts = [True, False] if response_json else [False]
                for use_json_mode in attempts:
                    try:
                        content = self._call_once(p, model, messages, temperature, use_json_mode)
                        self.last_used = {"provider": p.name, "model": model}
                        return content
                    except Exception as e:
                        kind = classify_error(e)
                        errors.append(f"{p.name}/{model}: [{kind}] {e}")
                        if kind in ("quota", "auth"):
                            p.cooldown_until = time.time() + self.cooldown_seconds
                            break  # 换下一个 provider
                        if kind == "other" and use_json_mode and "response_format" not in str(e):
                            break  # 非格式问题的其它错误不重试
        raise RuntimeError("LLM 链全部失败：\n" + "\n".join(errors))


def parse_json(raw: str):
    """解析 LLM 返回的 JSON，容忍 ```json 包裹和首尾杂质。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").lstrip()
        if text[:4].lower() == "json":
            text = text[4:]
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError(f"LLM 未返回 JSON：{raw[:200]}")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        raise ValueError(f"LLM 返回 JSON 不完整：{raw[:200]}")
    return json.loads(text[start:end + 1])


def chat_json(chain: LLMChain, messages, temperature: float = 0.2):
    return parse_json(chain.chat(messages, temperature=temperature, response_json=True))
