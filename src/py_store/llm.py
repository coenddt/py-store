"""插拔式 LLM 注册表 —— 《AI能力接入设计-L1问数档.md》§4.2 决策 D6。

协议（唯一）：``async def llm(messages: list[dict]) -> str``
py-store 不引入任何 LLM SDK（依赖由宿主应用决定）；护栏在 core/Host 不在 LLM，
能力开关只影响首次命中率，不影响正确性。

插拔三件套：
  - :func:`register_llm`  注册；client 为符合协议的函数（测试注入假 LLM 同走此口）
  - :func:`get_llm`       按名取用；未注册显式报错（禁静默回落默认厂商）
  - :func:`make_openai_compat`  OpenAI 兼容通用工厂：一个实现覆盖 DeepSeek/OpenAI/
    Moonshot/SiliconFlow/Ollama 等兼容端点；Anthropic 原生等特殊协议由应用侧自行
    ``make_llm`` 后 :func:`register_llm`，数据层不内置 SDK。

能力差异收敛为工厂开关：
  - ``json_mode=False``：不传 response_format，退化路径 = prompt 约定 + ask() 严格
    JSON 解析失败回喂（D3）；
  - ``effort=None``：不传 reasoning_effort（非推理模型或不支持该参数的端点）。

实测接入事实（原型阶段实证，直接继承；见设计文档 §4.2「三个接入事实」）：
  1. DeepSeek 网关对默认 ``Python-urllib/N`` UA 断连（curl 同参 200 实证）→ 显式覆盖 UA；
  2. ``json_object`` 模式要求 prompt 含 "json" 字样，缺失时网关 400 → 工厂契约预检，
     缺失即抛 ``llmJsonPromptMissing``（禁静默替调用方改写消息面，no-error-masking）；
  3. 网络层断连/重置 → ``llmNetworkError``；HTTP 4xx/5xx → ``llmHttpError``（携状态码
     与响应体片段）；空 content（多为推理 token 耗尽 max_tokens）→ ``llmEmptyContent``
     （携 finish_reason 与推理 token 数，禁静默返回空串）。
     三者均以 ``RuntimeError(dict)`` 结构化抛出，ask() 原样穿透（不回喂、不降级）。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Callable

# async def(messages: list[dict]) -> str
LlmClient = Callable[[list[dict]], Any]

_REGISTRY: dict[str, LlmClient] = {}


def register_llm(name: str, client: LlmClient) -> None:
    """注册一个 LLM 客户端；同名重复注册显式报错（禁静默覆盖）"""
    if name in _REGISTRY:
        raise ValueError(f'LLM 客户端重复注册: {name}')
    _REGISTRY[name] = client


def get_llm(name: str) -> LlmClient:
    """按注册名取客户端；未注册显式报错（禁静默回落默认厂商）"""
    if name not in _REGISTRY:
        raise KeyError(f'LLM 客户端未注册: {name}（已注册: {sorted(_REGISTRY)}）')
    return _REGISTRY[name]


def make_openai_compat(
    *,
    base_url: str,
    model: str,
    api_key: str,
    json_mode: bool = True,
    effort: str | None = 'low',
    max_tokens: int = 4096,
    timeout_s: int = 90,
) -> LlmClient:
    """OpenAI 兼容端点通用工厂（返回符合协议的 async llm；标准库零依赖）。

    失败全部显式抛错（``RuntimeError(dict)`` 结构化），禁静默返回空串——
    空 content 的实证成因与防御见模块 docstring 与设计文档 W3。
    """

    async def llm(messages: list[dict]) -> str:
        body: dict = {'model': model, 'messages': messages, 'max_tokens': max_tokens}
        if json_mode:
            # 契约预检：json_object 要求 prompt 含 "json" 字样（DeepSeek 实测 400 实证）。
            # 缺失显式报错交上游装配修复，禁静默改写调用方消息面（no-error-masking）
            joined = ' '.join(str(m.get('content', '')) for m in messages).lower()
            if 'json' not in joined:
                raise RuntimeError(
                    {
                        'code': 'llmJsonPromptMissing',
                        'model': model,
                        'message': "json_mode=True 要求 messages（system/user）中包含 'json' 字样",
                        'hint': "ask() 的 system 输出契约模板已固定含 'JSON' 措辞；"
                                '自定义 knowledge 时须保留',
                    }
                )
            body['response_format'] = {'type': 'json_object'}
        if effort is not None:
            body['reasoning_effort'] = effort

        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
                # 默认 UA「Python-urllib/N」被 DeepSeek 网关断连（curl 同参 200 实证），显式覆盖
                'User-Agent': 'py-store-ask/0.1',
            },
            method='POST',
        )

        def _call():
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    return json.loads(resp.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                detail = e.read().decode('utf-8', errors='replace')
                raise RuntimeError(
                    {'code': 'llmHttpError', 'status': e.code, 'model': model,
                     'body': detail[:500]}
                ) from None
            except OSError as e:
                # RemoteDisconnected / ConnectionReset 等网络层失败，同样结构化暴露
                raise RuntimeError(
                    {'code': 'llmNetworkError', 'model': model, 'detail': str(e)}
                ) from None

        data = await asyncio.to_thread(_call)
        choice = (data.get('choices') or [{}])[0]
        content = (choice.get('message') or {}).get('content')
        if not content:
            raise RuntimeError(
                {
                    'code': 'llmEmptyContent',
                    'model': model,
                    'finish_reason': choice.get('finish_reason'),
                    'reasoning_tokens': (
                        data.get('usage', {}).get('completion_tokens_details', {}) or {}
                    ).get('reasoning_tokens'),
                    'message': (
                        'LLM 返回空 content（多为 max_tokens 被推理耗尽，'
                        '调大 max_tokens 或降 effort）'
                    ),
                }
            )
        return content

    return llm
