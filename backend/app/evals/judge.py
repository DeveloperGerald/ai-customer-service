"""LLM-as-judge 客户端（OpenAI 兼容 Chat Completions 协议）。

模型 / api_key / base_url 全部可配置（EVALUATION__JUDGE_*）；
缺省时继承主聊天模型（llm.openai_* / llm.chat_model），后期可整体切换。
"""

from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings


class JudgeOutputError(Exception):
    """Judge 模型两次尝试后仍未返回可解析 JSON。"""


class JudgeClient:
    """裁判模型客户端：要求输出强约束 JSON，内置 1 次重试。"""

    def __init__(self, settings: Settings) -> None:
        eval_cfg = settings.evaluation
        llm_cfg = settings.llm
        self.model = eval_cfg.judge_model or llm_cfg.chat_model
        api_key = eval_cfg.judge_api_key or llm_cfg.openai_api_key
        self._api_key = api_key.get_secret_value() if api_key is not None else None
        self._base_url = eval_cfg.judge_base_url or llm_cfg.openai_base_url
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=30.0,
            )
        return self._client

    async def score_json(self, *, system: str, user: str) -> dict[str, Any]:
        """调用裁判模型并解析 JSON；解析失败重试 1 次，仍失败抛 JudgeOutputError。"""
        raw = await self._chat(system, user)
        parsed = _extract_json(raw)
        if parsed is None:
            raw = await self._chat(system, user)
            parsed = _extract_json(raw)
        if parsed is None:
            raise JudgeOutputError(f"judge 返回无法解析：{raw[:200]}")
        return parsed

    async def _chat(self, system: str, user: str) -> str:
        resp = await self._get_client().chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出中提取第一个 JSON 对象。"""
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        candidate = match.group(0) if match else stripped
    try:
        obj = json.loads(candidate)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None
