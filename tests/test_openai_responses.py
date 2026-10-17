"""Native Responses conversion, stateless tool history and failure boundaries."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.tools.function_tool import FunctionTool
from google.genai import types

from cargomind import config
from cargomind.healing import breaker
from cargomind.healing import openai_responses as adapter
from cargomind.healing.offline_llm import OfflineLlm, OfflinePolicy
from cargomind.healing.resilient_llm import build_model


def route(origin: str, destination: str, avoid_tolls: bool = False) -> dict:
    """Compute a route between two operational city identifiers."""
    return {"origin": origin, "destination": destination}


def request(parts=None, **cfg):
    return LlmRequest(
        contents=[types.Content(role="user", parts=parts or [types.Part(text="route Mumbai to Pune")])],
        tools_dict={"route": FunctionTool(route)},
        config=types.GenerateContentConfig(**cfg),
    )


def response(output=None, **overrides):
    result = {
        "status": "completed", "model": "gpt-6.1-sol-actual",
        "output": output if output is not None else [{"type": "message", "content": [{"type": "output_text", "text": "A route is available."}]}],
        "usage": {"input_tokens": 19, "output_tokens": 12, "total_tokens": 31,
                  "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 5}},
    }
    result.update(overrides)
    return result


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(calls=[], constructors=[], outputs=[response()])

    class Client:
        def __init__(self, **kwargs):
            state.constructors.append(kwargs)
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def create(self, **kwargs):
            state.calls.append(kwargs)
            output = state.outputs.pop(0)
            if isinstance(output, Exception):
                raise output
            return SimpleNamespace(model_dump=lambda **kwargs: output)

    monkeypatch.setattr(adapter, "AsyncOpenAI", Client)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "mock-not-a-key")
    return state


def collect(model, req):
    async def run():
        return [item async for item in model.generate_content_async(req, stream=True)]
    return asyncio.run(run())


def test_native_request_response_usage_and_unsupported_sampling(sdk):
    out = collect(adapter.OpenAIResponsesLlm(), request(temperature=0.2, top_p=0.9, max_output_tokens=20000, system_instruction="Use tool evidence."))[0]
    call = sdk.calls[0]
    assert call["model"] == "gpt-6.1-sol"
    assert call["store"] is False and call["stream"] is False
    assert call["instructions"] == "Use tool evidence."
    assert call["max_output_tokens"] == 4096
    assert "temperature" not in call and "top_p" not in call
    assert sdk.constructors[0]["max_retries"] == 0
    assert sdk.constructors[0]["timeout"] == 45
    assert out.content.parts[0].text == "A route is available."
    assert out.custom_metadata["served_by"] == "openai/gpt-6.1-sol-actual"
    assert out.model_version == "gpt-6.1-sol-actual"
    assert out.usage_metadata.prompt_token_count == 19
    assert out.usage_metadata.candidates_token_count == 12
    assert out.usage_metadata.thoughts_token_count == 5
    assert out.usage_metadata.cached_content_token_count == 3
    assert out.usage_metadata.total_token_count == 31
    tool = call["tools"][0]
    assert tool["type"] == "function" and tool["strict"] is False
    assert tool["parameters"]["properties"]["avoid_tolls"]["type"] == "boolean"
    assert "avoid_tolls" not in tool["parameters"].get("required", [])


def test_parallel_tool_calls_outputs_and_encrypted_reasoning_roundtrip(sdk):
    sdk.outputs = [response(output=[
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"},
        {"type": "function_call", "call_id": "call_a", "name": "route", "arguments": '{"origin":"Mumbai","destination":"Pune"}'},
        {"type": "function_call", "call_id": "call_b", "name": "route", "arguments": '{"origin":"Delhi","destination":"Agra"}'},
    ]), response()]
    model = adapter.OpenAIResponsesLlm()
    req = request()
    first = collect(model, req)[0]
    assert [part.function_call.id for part in first.content.parts] == ["call_a", "call_b"]
    assert first.content.parts[0].thought_signature
    assert not any(part.text for part in first.content.parts)
    req.contents.extend([first.content, types.Content(role="user", parts=[
        types.Part(function_response=types.FunctionResponse(id="call_b", name="route", response={"status": "ok", "km": 200})),
        types.Part(function_response=types.FunctionResponse(id="call_a", name="route", response={"status": "ok", "km": 150})),
    ])])
    collect(model, req)
    items = sdk.calls[-1]["input"]
    assert items[1] == {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
    assert [item["call_id"] for item in items if item.get("type") == "function_call_output"] == ["call_b", "call_a"]
    assert [item["call_id"] for item in items if item.get("type") == "function_call"] == ["call_a", "call_b"]
    # Opaque reasoning belongs to its source model and must not reach a fallback.
    collect_inputs = adapter._inputs(req, "gpt-5-mini")
    assert not any(item.get("type") == "reasoning" for item in collect_inputs)


def test_legacy_missing_ids_are_paired_by_name_fifo(sdk):
    req = request()
    req.contents.extend([
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="route", args={"origin": "A", "destination": "B"}))]),
        types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(name="route", response={"status": "ok"}))]),
    ])
    collect(adapter.OpenAIResponsesLlm(), req)
    calls = sdk.calls[0]["input"]
    assert calls[-2]["call_id"].startswith("call_")
    assert calls[-1]["call_id"] == calls[-2]["call_id"]


@pytest.mark.parametrize("output", [
    [{"type": "function_call", "call_id": "call_a", "name": "route", "arguments": "[]"}],
    [{"type": "function_call", "call_id": "call_a", "name": "unknown", "arguments": "{}"}],
    [{"type": "function_call", "call_id": "call_a", "name": "route", "arguments": '{"x":NaN}'}],
    [{"type": "function_call", "name": "route", "arguments": "{}"}],
    [{"type": "function_call", "call_id": "duplicate", "name": "route", "arguments": "{}"},
     {"type": "function_call", "call_id": "duplicate", "name": "route", "arguments": "{}"}],
    [],
])
def test_invalid_response_rejected_before_yield(sdk, output):
    sdk.outputs = [response(output=output)]
    with pytest.raises(ValueError):
        collect(adapter.OpenAIResponsesLlm(), request())


def test_incomplete_and_schema_and_media_fail_closed(sdk):
    sdk.outputs = [response(status="incomplete")]
    with pytest.raises(ValueError, match="did not complete"):
        collect(adapter.OpenAIResponsesLlm(), request())
    with pytest.raises(ValueError, match="response schema"):
        collect(adapter.OpenAIResponsesLlm(), request(response_json_schema={"type": "object"}))
    with pytest.raises(ValueError, match="text and tool results"):
        collect(adapter.OpenAIResponsesLlm(), request([types.Part(inline_data=types.Blob(data=b"image", mime_type="image/png"))]))


def test_orphan_and_duplicate_history_rejected(sdk):
    with pytest.raises(ValueError, match="no matching call"):
        collect(adapter.OpenAIResponsesLlm(), request([types.Part(function_response=types.FunctionResponse(id="unseen", name="route", response={}))]))
    part = types.Part(function_call=types.FunctionCall(id="duplicate", name="route", args={}))
    with pytest.raises(ValueError, match="Duplicate"):
        collect(adapter.OpenAIResponsesLlm(), request([part, part]))


def test_disabled_and_restricted_tools_and_history_replay_rejected(sdk):
    returned = [{"type": "function_call", "call_id": "call_new", "name": "route", "arguments": "{}"}]
    sdk.outputs = [response(output=returned), response(output=returned), response(output=returned)]
    with pytest.raises(ValueError, match="undeclared"):
        collect(adapter.OpenAIResponsesLlm(), request(tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="NONE"))))
    with pytest.raises(ValueError, match="undeclared"):
        collect(adapter.OpenAIResponsesLlm(), request(tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="ANY", allowed_function_names=["other"]))))
    req = request()
    req.contents.extend([
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(id="call_new", name="route", args={}))]),
        types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(id="call_new", name="route", response={"status": "ok"}))]),
    ])
    with pytest.raises(ValueError, match="duplicate"):
        collect(adapter.OpenAIResponsesLlm(), req)


def test_openai_only_role_chains_and_no_key_offline(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "FORCE_OFFLINE", False)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "mock-not-a-key")
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "mock-google")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "mock-anthropic")
    assert [item.model for item in build_model("fast").chain] == ["openai/gpt-6-luna", "openai/gpt-5-mini", "offline/rule-engine"]
    assert [item.model for item in build_model("pro").chain] == ["openai/gpt-6.1-sol", "openai/gpt-5-mini", "offline/rule-engine"]
    assert build_model("critic").chain[0].model == "openai/gpt-6.1-sol"
    assert config.mode_summary()["gemini"] is False
    assert config.mode_summary()["claude"] is False
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    assert isinstance(build_model("fast").chain[0], OfflineLlm)


def test_native_failure_falls_to_verified_backup_preserving_actual_model(sdk, monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "FORCE_OFFLINE", False)
    breaker.reset_all(forget=True)
    sdk.outputs = [ValueError("model not found"), response(model="gpt-5-mini-actual")]
    out = collect(build_model("pro", OfflinePolicy(agent="test")), request())[0]
    assert [call["model"] for call in sdk.calls] == ["gpt-6.1-sol", "gpt-5-mini"]
    assert out.custom_metadata["served_by"] == "openai/gpt-5-mini-actual"
    assert out.custom_metadata["fallbacks"] == 1


def test_actual_adk_runner_executes_tool_and_preserves_session_history(sdk):
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    sdk.outputs = [response(output=[
        {"type": "reasoning", "id": "rs_runner", "summary": [], "encrypted_content": "opaque-runner"},
        {"type": "function_call", "call_id": "call_runner", "name": "route", "arguments": '{"origin":"Mumbai","destination":"Pune"}'},
    ]), response(), response()]

    async def run():
        runner = Runner(agent=LlmAgent(name="native_test", model=adapter.OpenAIResponsesLlm(), tools=[route]),
                        app_name="native_test", session_service=InMemorySessionService(), auto_create_session=True)
        first = [event async for event in runner.run_async(user_id="operator", session_id="test_native",
                 new_message=types.Content(role="user", parts=[types.Part(text="Route Mumbai to Pune")]))]
        second = [event async for event in runner.run_async(user_id="operator", session_id="test_native",
                  new_message=types.Content(role="user", parts=[types.Part(text="Repeat the destination")]))]
        return first, second

    first, second = asyncio.run(run())
    results = [result for event in first for result in event.get_function_responses() or []]
    assert results[0].response["destination"] == "Pune"
    assert results[0].id == "call_runner"
    assert any(event.is_final_response() for event in first)
    assert any(event.is_final_response() for event in second)
    assert len(sdk.calls) == 3
    assert any(item.get("type") == "function_call_output" and item["call_id"] == "call_runner" for item in sdk.calls[-1]["input"])
    assert any(item.get("type") == "reasoning" for item in sdk.calls[-1]["input"])
