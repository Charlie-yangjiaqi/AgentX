"""OpenAI 兼容 Provider：通过 httpx 调用 Chat Completions API。

支持任意 OpenAI-compatible 端点（OpenAI / 本地网关 / vLLM / DeepSeek 等），
只需配置 base_url 与 api_key。

可观测性（Phase 6.8）：
- LLMRequestError：结构化错误（category/detail/status/error_body/request_id/trace_id），
  链路固定 Provider → Service → MCP/CLI formatter（上游不做字符串解析）
- llm_trace：每次调用结构化 logging（trace_id 关联一次完整调用）
- 错误分类：invalid_model / invalid_parameter / authentication / rate_limit /
  context_length / provider_error / request_timeout / provider_cancel / client_cancel
- 能力检查：MODEL_CAPABILITIES（未来扩展 tool_call/vision/json_mode）
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections import Counter
from typing import Any

import httpx

from agentx.providers.messages import ChatMessage, ModelResponse, ToolCall, ToolSpec, Usage

logger = logging.getLogger("agentx.providers")

# ---------- 错误分类常量 ----------

CAT_INVALID_MODEL = "invalid_model"
CAT_INVALID_PARAMETER = "invalid_parameter"
CAT_AUTHENTICATION = "authentication"
CAT_RATE_LIMIT = "rate_limit"
CAT_CONTEXT_LENGTH = "context_length"
CAT_PROVIDER_ERROR = "provider_error"
CAT_REQUEST_TIMEOUT = "request_timeout"
CAT_PROVIDER_CANCEL = "provider_cancel"
CAT_CLIENT_CANCEL = "client_cancel"
CAT_UNKNOWN = "unknown"

# invalid_parameter 关键字优先级高于 invalid_model：
# DeepSeek 常返回 "model exists, parameter unsupported"，按 model 关键词会误判
_PARAM_KEYWORDS = (
    "reasoning_effort",
    "temperature",
    "max_tokens",
    "generation",
    "response_format",
    "frequency_penalty",
    "presence_penalty",
    "top_p",
    "tools",
    "stop",
    "seed",
)
_MODEL_KEYWORDS = ("model not found", "unknown model", "invalid model", "model does not exist")
_CONTEXT_KEYWORDS = ("context length", "maximum context", "too many tokens", "token limit")

# ---------- 模型能力表（Provider Layer 拥有模型能力知识） ----------

MODEL_CAPABILITIES: dict[str, dict[str, bool]] = {
    "deepseek-v4-pro": {"reasoning_effort": True},
    "deepseek-v4-flash": {"reasoning_effort": True},
    # 未来扩展：{"tool_call": True, "vision": False, "json_mode": True, ...}
}

# ---------- trace id ----------

_trace_counter = itertools.count(1)


def new_trace_id() -> str:
    """唯一 trace id：llm_20260829_143000_0001（关联一次完整调用）。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"llm_{ts}_{next(_trace_counter):04d}"


def check_reasoning_effort_compat(model: str, reasoning_effort: Any) -> str | None:
    """Capability 检查：模型是否支持 reasoning_effort。

    返回 warning 文本；支持或未启用时返回 None。不阻塞调用，
    只是提前告知（避免等 API 400 才知道）。
    """
    if reasoning_effort is None:
        return None
    caps = MODEL_CAPABILITIES.get(model)
    if caps is None:
        return (
            f"model {model} may not support reasoning_effort={reasoning_effort} "
            "(unknown model capabilities)"
        )
    if not caps.get("reasoning_effort"):
        return f"model {model} may not support reasoning_effort={reasoning_effort}"
    return None


def _classify_error(status: int | None, body: str, error: Exception | None) -> str:
    """错误分类（不含网络/超时时的传输层判断）。"""
    body_l = body.lower()
    if status == 401 or status == 403:
        return CAT_AUTHENTICATION
    if status == 429:
        return CAT_RATE_LIMIT
    if status == 413:
        return CAT_CONTEXT_LENGTH
    if status in (400, 422):
        # invalid_parameter 优先（参数关键字）
        if any(k in body_l for k in _PARAM_KEYWORDS):
            return CAT_INVALID_PARAMETER
        if any(k in body_l for k in _MODEL_KEYWORDS):
            return CAT_INVALID_MODEL
        if any(k in body_l for k in _CONTEXT_KEYWORDS):
            return CAT_CONTEXT_LENGTH
        return CAT_INVALID_PARAMETER
    if status is not None and status >= 500:
        return CAT_PROVIDER_ERROR
    return CAT_UNKNOWN


def _classify_transport_error(error: Exception) -> str:
    """传输层错误分类（httpx.TransportError 子类）。"""
    if isinstance(error, httpx.TimeoutException):
        return CAT_REQUEST_TIMEOUT
    if isinstance(error, (httpx.CloseError, httpx.RemoteProtocolError, httpx.ReadError)):
        return CAT_PROVIDER_CANCEL
    return CAT_PROVIDER_ERROR


class LLMRequestError(RuntimeError):
    """结构化 LLM 调用错误。

    链路固定：Provider 构造 → Service 透传 → MCP/CLI formatter 展示。
    上游（MCP/CLI）不得通过字符串匹配判断错误类型。
    """

    def __init__(
        self,
        *,
        category: str,
        detail: str,
        provider: str,
        model: str,
        status: int | None = None,
        endpoint: str = "",
        error_body: str = "",
        request_id: str = "",
        trace_id: str = "",
        latency: float | None = None,
    ) -> None:
        self.category = category
        self.detail = detail
        self.provider = provider
        self.model = model
        self.status = status
        self.endpoint = endpoint
        self.error_body = error_body
        self.request_id = request_id
        self.trace_id = trace_id
        self.latency = latency
        super().__init__(detail)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "detail": self.detail,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "endpoint": self.endpoint,
            "error_body": self.error_body,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "latency": self.latency,
        }


class OpenAIProvider:
    """OpenAI-compatible chat completions 客户端。

    统一处理超时、重试（指数退避）与错误映射。
    4xx（参数/鉴权）不重试；5xx / 429 / 传输层错误自动重试。
    所有失败都抛 LLMRequestError（结构化），成功路径不变。
    """

    def __init__(
        self,
        name: str = "openai",
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 180.0,
        max_retries: int = 5,
        retry_backoff: float = 1.0,
        trust_env: bool = True,
        generation: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._trust_env = trust_env
        self._generation = generation or {}
        self._transport = transport
        self._extra_headers = headers
        self._client: httpx.AsyncClient | None = None
        self._tool_name_map: dict[str, str] = {}

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            from agentx.http import with_default_user_agent

            headers: dict[str, str] = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            if self._extra_headers:
                headers.update(self._extra_headers)
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=with_default_user_agent(headers),
                transport=self._transport,
                trust_env=self._trust_env,
                # 不复用 keep-alive 连接：代理隧道常被中间层静默关闭，
                # 复用死隧道会表现为 "Server disconnected" 假断连
                limits=httpx.Limits(max_keepalive_connections=0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- 主调用 ----------

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ToolSpec] | None = None,
        **options: object,
    ) -> ModelResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": [_to_api_message(m) for m in messages],
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": _to_api_tool_name(t.name),
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            self._tool_name_map = {_to_api_tool_name(t.name): t.name for t in tools}
        else:
            self._tool_name_map = {}
        body.update(self._generation)
        body.update(options)

        trace_id = new_trace_id()
        request_summary = self._summarize_request(messages, model, tools, options)
        t0 = time.monotonic()
        last_error: LLMRequestError | None = None

        for attempt in range(self._max_retries + 1):
            try:
                resp = await self.client.post("/chat/completions", json=body)
                resp.raise_for_status()
                parsed = _parse_response(resp.json(), self._tool_name_map)
                self._log_trace(
                    trace_id,
                    request_summary,
                    status="success",
                    latency=time.monotonic() - t0,
                    usage=parsed.usage,
                )
                return parsed
            except httpx.HTTPStatusError as e:
                err = self._build_http_error(trace_id, e.response, model, t0)
                # 参数/鉴权类错误不重试（再试也是同样结果）
                if err.category in {
                    CAT_AUTHENTICATION,
                    CAT_INVALID_MODEL,
                    CAT_INVALID_PARAMETER,
                    CAT_CONTEXT_LENGTH,
                }:
                    self._log_trace(
                        trace_id,
                        request_summary,
                        status="failed",
                        latency=time.monotonic() - t0,
                        error=err,
                    )
                    raise err from e
                last_error = err
            except httpx.TransportError as e:
                category = _classify_transport_error(e)
                last_error = LLMRequestError(
                    category=category,
                    detail=f"传输错误: {type(e).__name__}: {e}",
                    provider=self.name,
                    model=model,
                    trace_id=trace_id,
                    latency=time.monotonic() - t0,
                )
            except asyncio.CancelledError:
                # 客户端取消：记录 trace 后重新抛出（不吞）
                self._log_trace(
                    trace_id,
                    request_summary,
                    status="failed",
                    latency=time.monotonic() - t0,
                    category=CAT_CLIENT_CANCEL,
                )
                raise
            if attempt < self._max_retries:
                await asyncio.sleep(self._retry_backoff * (2**attempt))

        # 重试耗尽：透传最后一次错误（结构化，含分类）
        last = last_error or LLMRequestError(
            category=CAT_UNKNOWN,
            detail="模型调用失败（未知错误）",
            provider=self.name,
            model=model,
            trace_id=trace_id,
        )
        final = LLMRequestError(
            category=last.category,
            detail=f"模型调用失败（重试 {self._max_retries} 次）: {last.detail}",
            provider=self.name,
            model=model,
            status=last.status,
            endpoint=last.endpoint,
            error_body=last.error_body,
            request_id=last.request_id,
            trace_id=trace_id,
            latency=time.monotonic() - t0,
        )
        self._log_trace(
            trace_id, request_summary, status="failed", latency=time.monotonic() - t0, error=final
        )
        raise final

    # ---------- 可观测性辅助 ----------

    def _build_http_error(
        self, trace_id: str, resp: httpx.Response, model: str, t0: float
    ) -> LLMRequestError:
        status = resp.status_code
        body_text = (resp.text or "")[:2000]
        category = _classify_error(status, body_text, None)
        request_id = resp.headers.get("x-request-id") or resp.headers.get("request-id") or ""
        detail = body_text.strip()[:500] or f"HTTP {status}"
        return LLMRequestError(
            category=category,
            detail=detail,
            provider=self.name,
            model=model,
            status=status,
            endpoint=f"{self._base_url}/chat/completions",
            error_body=body_text,
            request_id=request_id,
            trace_id=trace_id,
            latency=time.monotonic() - t0,
        )

    def _summarize_request(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ToolSpec] | None,
        options: dict[str, object],
    ) -> dict[str, Any]:
        """请求摘要（不含消息内容、不含 api_key）。"""
        roles = Counter(m.role for m in messages)
        summary: dict[str, Any] = {
            "provider": self.name,
            "model": model,
            "messages": len(messages),
            "roles": dict(roles),
            "tools": len(tools or []),
        }
        if self._generation:
            summary["generation"] = {
                k: (v if not isinstance(v, (bytes, bytearray)) else "<bytes>")
                for k, v in self._generation.items()
            }
        if options:
            summary["options"] = {
                k: (v if not isinstance(v, (bytes, bytearray)) else "<bytes>")
                for k, v in options.items()
            }
        return summary

    def _log_trace(
        self,
        trace_id: str,
        request_summary: dict[str, Any],
        *,
        status: str,
        latency: float,
        usage: Usage | None = None,
        error: LLMRequestError | None = None,
        category: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "trace_id": trace_id,
            "status": status,
            "latency_ms": round(latency * 1000, 1),
            **request_summary,
        }
        if usage is not None:
            entry["usage"] = {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            }
        if error is not None:
            entry["category"] = error.category
            if error.status is not None:
                entry["status_code"] = error.status
            if error.request_id:
                entry["request_id"] = error.request_id
            if error.detail:
                entry["error"] = error.detail[:500]
        elif category is not None:
            entry["category"] = category
        logger.info("llm_trace %s", json.dumps(entry, ensure_ascii=False))


def _to_api_message(msg: ChatMessage) -> dict[str, Any]:
    api: dict[str, Any] = {"role": msg.role, "content": msg.content}
    if msg.tool_calls:
        api["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": _json(tc.arguments)},
            }
            for tc in msg.tool_calls
        ]
    if msg.tool_call_id is not None:
        api["tool_call_id"] = msg.tool_call_id
    return api


def _parse_response(
    data: dict[str, Any], tool_name_map: dict[str, str] | None = None
) -> ModelResponse:
    choice = data["choices"][0]
    message = choice["message"]
    content = message.get("content")
    tool_calls = [
        ToolCall(
            id=tc["id"],
            name=_restore_tool_name(tc["function"]["name"], tool_name_map or {}),
            arguments=_unjson(tc["function"].get("arguments")),
        )
        for tc in (message.get("tool_calls") or [])
    ]
    usage = data.get("usage") or {}
    return ModelResponse(
        content=content,
        tool_calls=tool_calls,
        usage=Usage(
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        ),
    )


def _to_api_tool_name(name: str) -> str:
    """工具名适配：DeepSeek 等官方 API 只允许 [a-zA-Z0-9_-]，点号转下划线。"""
    return name.replace(".", "_")


def _restore_tool_name(api_name: str, tool_name_map: dict[str, str]) -> str:
    """按发送时的映射还原内部工具名；映射外（模型编造的名字）原样返回。"""
    return tool_name_map.get(api_name, api_name)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _unjson(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
