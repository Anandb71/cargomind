"""Native, stateless OpenAI Responses API bridge for Google ADK.

Tool call IDs and opaque encrypted reasoning are preserved across tool turns.
Sampling controls are deliberately omitted: reasoning models have different
support for them. ResilientLlm owns retries and the deterministic last fallback.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from openai import AsyncOpenAI
from pydantic import Field

from cargomind import config

_SIGNATURE = b"cargomind-openai-responses-v1:"
logger = logging.getLogger(__name__)


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json", exclude_none=True) if hasattr(value, "model_dump") else value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _record(model: str, usage: dict | None, elapsed_ms: float, status: str) -> None:
    try:
        from cargomind.telemetry import record_model_call
        record_model_call(model, usage, elapsed_ms, kind="agent", status=status)
    except Exception:  # noqa: BLE001 - telemetry never changes model behavior
        # Usage receipts must not change generation or fallback behavior.
        logger.debug("OpenAI usage receipt was unavailable")


def _schema(value: Any) -> Any:
    """Translate GenAI's schema enum spelling without changing required fields."""
    value = _dump(value)
    if isinstance(value, list):
        return [_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _schema(item) for key, item in value.items() if key not in ("nullable", "property_ordering", "propertyOrdering")}
    if isinstance(result.get("type"), str):
        result["type"] = result["type"].lower()
    if value.get("nullable") and result.get("type"):
        result["type"] = [result["type"], "null"]
    return result


def _tool_declarations(request: LlmRequest) -> list[dict[str, Any]]:
    declarations: dict[str, Any] = {}
    for tool in request.config.tools or []:
        for declaration in getattr(tool, "function_declarations", None) or []:
            declarations[declaration.name] = declaration
    for name, tool in request.tools_dict.items():
        declaration = tool._get_declaration()
        if declaration is not None:
            declarations[name] = declaration
    result = []
    for name, declaration in declarations.items():
        parameters = declaration.parameters_json_schema or declaration.parameters
        result.append({
            "type": "function", "name": name,
            "description": declaration.description or "",
            "parameters": _schema(parameters) if parameters is not None else {"type": "object", "properties": {}},
            # ADK tools may contain optional/default arguments. Do not let the
            # Responses default strict normalization silently make them required.
            "strict": False,
        })
    return result


def _instruction(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, types.Content):
        return "\n".join(part.text for part in value.parts or [] if part.text)
    if isinstance(value, list):
        return "\n".join(_instruction(item) for item in value)
    raise ValueError("Unsupported OpenAI system instruction type")


def _inputs(request: LlmRequest, model: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    pending: list[tuple[str, str]] = []
    seen_calls: set[str] = set()
    seen_reasoning: set[str] = set()
    for content_index, content in enumerate(request.contents):
        for part_index, part in enumerate(content.parts or []):
            signature = part.thought_signature
            if signature and signature.startswith(_SIGNATURE):
                packet = json.loads(signature[len(_SIGNATURE):])
                if packet.get("model") == model:
                    for item in packet.get("items", []):
                        identity = item.get("id") or item.get("encrypted_content")
                        if item.get("type") == "reasoning" and item.get("encrypted_content") and identity not in seen_reasoning:
                            result.append(item)
                            seen_reasoning.add(identity)
            if part.function_call is not None:
                call = part.function_call
                if not call.name:
                    raise ValueError("OpenAI function call is missing its name")
                arguments = _json(call.args or {})
                call_id = call.id or "call_" + hashlib.sha256(f"{content_index}:{part_index}:{call.name}:{arguments}".encode()).hexdigest()[:24]
                if call_id in seen_calls:
                    raise ValueError("Duplicate OpenAI function call ID")
                result.append({"type": "function_call", "call_id": call_id, "name": call.name, "arguments": arguments})
                pending.append((call_id, call.name))
                seen_calls.add(call_id)
            elif part.function_response is not None:
                response = part.function_response
                matches = [pair for pair in pending if (pair[0] == response.id if response.id else pair[1] == response.name)]
                if not matches:
                    raise ValueError("OpenAI function output has no matching call")
                call_id, _ = matches[0]
                pending.remove(matches[0])
                result.append({"type": "function_call_output", "call_id": call_id, "output": _json(response.response or {})})
            elif part.text:
                if not part.thought:
                    result.append({"role": "assistant" if content.role == "model" else "user", "content": part.text})
            elif part.inline_data is not None or part.file_data is not None:
                raise ValueError("OpenAI operational agent accepts text and tool results only")
    return result


class OpenAIResponsesLlm(BaseLlm):
    """Responses tool calling; each generation emits one complete ADK response."""

    model: str = "openai/gpt-6.1-sol"
    reasoning_effort: str = "low"
    timeout_seconds: float = Field(default=45.0, ge=1, le=120)
    max_output_tokens: int = Field(default=4096, ge=128, le=16384)

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=False)

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        model = self.model.removeprefix("openai/")
        cfg = llm_request.config
        if cfg.response_schema is not None or cfg.response_json_schema is not None:
            raise ValueError("OpenAI adapter requires tool results rather than an ADK response schema")
        tools = _tool_declarations(llm_request)
        kwargs: dict[str, Any] = {
            "model": model, "input": _inputs(llm_request, model),
            "store": False, "stream": False,
            "reasoning": {"effort": self.reasoning_effort},
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": min(cfg.max_output_tokens or self.max_output_tokens, self.max_output_tokens),
        }
        instruction = _instruction(cfg.system_instruction)
        if instruction:
            kwargs["instructions"] = instruction
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = True
            tool_config = cfg.tool_config
            calling = tool_config.function_calling_config if tool_config else None
            if calling:
                mode = getattr(calling.mode, "value", calling.mode)
                if mode == "NONE":
                    kwargs["tool_choice"] = "none"
                elif mode == "ANY":
                    allowed = calling.allowed_function_names or []
                    if len(allowed) == 1:
                        kwargs["tool_choice"] = {"type": "function", "name": allowed[0]}
                    elif allowed:
                        kwargs["tools"] = [tool for tool in tools if tool["name"] in allowed]
                        kwargs["tool_choice"] = "required"
                    else:
                        kwargs["tool_choice"] = "required"
        allowed_names = {tool["name"] for tool in kwargs.get("tools", [])}
        choice = kwargs.get("tool_choice")
        if choice == "none":
            allowed_names.clear()
        elif isinstance(choice, dict):
            allowed_names.intersection_update({choice["name"]})
        previous_call_ids = {item["call_id"] for item in kwargs["input"] if item.get("type") == "function_call"}
        started = time.perf_counter()
        try:
            async with AsyncOpenAI(api_key=config.OPENAI_API_KEY, max_retries=0, timeout=self.timeout_seconds) as client:
                response = await client.responses.create(**kwargs)
        except Exception:
            _record(model, None, (time.perf_counter() - started) * 1000, "failed")
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        raw = _dump(response)
        usage = raw.get("usage") or {}
        actual = raw.get("model") or model
        # Record actual billed usage even when the API returns an incomplete or
        # unusable answer. A completed API call is not an approval of its text.
        _record(actual, usage, elapsed_ms, "completed" if raw.get("status") in (None, "completed") else "failed")
        if raw.get("status") not in (None, "completed"):
            raise ValueError("OpenAI response did not complete")
        parts: list[types.Part] = []
        reasoning: list[dict[str, Any]] = []
        output_call_ids: set[str] = set()
        for item in raw.get("output", []):
            kind = item.get("type")
            if kind == "reasoning":
                if item.get("encrypted_content"):
                    reasoning.append({key: item[key] for key in ("type", "id", "encrypted_content", "summary") if key in item})
            elif kind == "function_call":
                name, call_id = item.get("name"), item.get("call_id")
                if not name or not call_id or name not in allowed_names:
                    raise ValueError("OpenAI returned an undeclared or uncorrelated function call")
                if call_id in output_call_ids or call_id in previous_call_ids:
                    raise ValueError("OpenAI returned duplicate function call IDs")
                output_call_ids.add(call_id)
                args = json.loads(item.get("arguments", "{}"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite tool argument")))
                if not isinstance(args, dict):
                    raise ValueError("OpenAI tool arguments must be a JSON object")
                part = types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args))
                if reasoning:
                    part.thought_signature = _SIGNATURE + _json({"model": model, "items": reasoning}).encode()
                    reasoning = []
                parts.append(part)
            elif kind == "message":
                for entry in item.get("content", []):
                    if entry.get("type") == "output_text" and entry.get("text"):
                        parts.append(types.Part(text=entry["text"]))
                    elif entry.get("type") == "refusal" and entry.get("refusal"):
                        parts.append(types.Part(text=entry["refusal"]))
        if not parts:
            raise ValueError("OpenAI returned no usable text or tool call")
        metadata = {"provider": "openai", "api": "responses", "requested_model": model, "served_by": "openai/" + actual, "elapsed_ms": round(elapsed_ms, 2), "usage": usage}
        yield LlmResponse(
            content=types.Content(role="model", parts=parts), partial=False,
            turn_complete=True, model_version=actual, custom_metadata=metadata,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=usage.get("input_tokens"), candidates_token_count=usage.get("output_tokens"),
                total_token_count=usage.get("total_tokens"),
                thoughts_token_count=(usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
                cached_content_token_count=(usage.get("input_tokens_details") or {}).get("cached_tokens"),
            ),
        )
