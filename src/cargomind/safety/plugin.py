"""ADK safety hooks. Register this plugin BEFORE SelfHealingPlugin.

Allowed hooks mutate sanitized objects and return None so later healing hooks
still run. Only rejected operations return a replacement to short-circuit tools.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from google.adk.models import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import AgentTool
from google.genai import types

from cargomind.safety.privacy import REFUSAL, inspect_input, inspect_output, sanitize

_WRITES = {"commit_dispatch_plan", "send_notification", "update_shipment_status", "create_reorder", "assign_vehicle", "remember_operator_feedback"}


def _text(content: Any) -> str:
    return "\n".join(part.text for part in (getattr(content, "parts", None) or []) if getattr(part, "text", None))


def _content(content: Any) -> Any:
    if content is None:
        return None
    cleaned = content.model_copy(deep=True)
    for part in cleaned.parts or []:
        if part.text is not None:
            part.text = sanitize(part.text)
            if part.text.startswith("For context"):
                verdict = inspect_output(part.text)
                if not verdict["allowed"] and "instruction_override" in verdict["reasons"]:
                    part.text = verdict["text"]
        if part.function_call:
            part.function_call.args = sanitize(dict(part.function_call.args or {}))
        if part.function_response:
            part.function_response.response = _evidence_value(dict(part.function_response.response or {}))
    return cleaned


def _response(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]), custom_metadata={"safety_blocked": True})


def _denied(reason: str) -> dict:
    return {"status": "error", "error": REFUSAL, "safety_blocked": True, "reasons": [reason]}


def _user_text(context: Any) -> str:
    return _text(getattr(context, "user_content", None))


def _evidence_value(value: Any) -> Any:
    """Remove injected instructions from untrusted tool facts, after privacy cleaning."""
    cleaned = sanitize(value)
    if isinstance(cleaned, dict):
        return {key: _evidence_value(item) for key, item in cleaned.items()}
    if isinstance(cleaned, list):
        return [_evidence_value(item) for item in cleaned]
    if isinstance(cleaned, str):
        verdict = inspect_input(cleaned)
        if not verdict["allowed"]:
            return REFUSAL
    return cleaned


def _invocation(context: Any) -> str:
    return str(getattr(context, "invocation_id", ""))


def _approved(name: str, args: dict, user: str) -> bool:
    """Approval comes from the invocation's user message, never model-supplied args."""
    if name == "remember_operator_feedback":
        return bool(re.search(r"\b(?:remember|always|never|prefer|wrong|correction|feedback)\b", user, re.IGNORECASE))
    if re.search(r"\b(?:do not|don't|never|not yet|without approval)\b", user, re.IGNORECASE):
        return False
    low = user.lower()
    command = re.sub(r"^(?:yes[,! ]*|please\s+|go ahead[,! ]*|i\s+)+", "", low).lstrip()
    if name == "commit_dispatch_plan":
        return bool(re.match(r"(?:commit|apply|approve)\b.{0,30}\b(?:plan|dispatch)\b", command))
    required = {"send_notification": ("shipment_id",), "update_shipment_status": ("shipment_id",), "create_reorder": ("sku", "warehouse_id"), "assign_vehicle": ("shipment_id", "vehicle_id")}.get(name, ())
    if not required or not all(str(args.get(key, "")).lower() in low and args.get(key) for key in required):
        return False
    verbs = {"send_notification": r"send|queue|approve", "update_shipment_status": r"mark|update|set", "create_reorder": r"raise|create|place|reorder|restock|approve", "assign_vehicle": r"assign|approve"}[name]
    if not re.match(rf"(?:{verbs})\b", command):
        return False
    return name != "update_shipment_status" or str(args.get("status", "")).replace("_", " ").lower() in low.replace("_", " ")


class SafetyPlugin(BasePlugin):
    def __init__(self, name: str = "simulation_safety") -> None:
        super().__init__(name=name)

    async def on_user_message_callback(self, *, invocation_context, user_message):
        verdict = inspect_input(_text(user_message))
        if not verdict["allowed"]:
            invocation_context.session.state["safety:blocked_invocation"] = invocation_context.invocation_id
            return types.Content(role="user", parts=[types.Part(text=verdict["text"])])
        return _content(user_message)

    async def before_run_callback(self, *, invocation_context):
        previous = invocation_context.session.state.get("safety:tool_evidence", {})
        parent = previous.get("invocation_id") if getattr(getattr(invocation_context, "agent", None), "name", "") != "cargomind_concierge" else None
        invocation_context.session.state["safety:tool_evidence"] = {"invocation_id": _invocation(invocation_context), "parent_invocation_id": parent, "tools": {}}
        if invocation_context.session.state.get("safety:blocked_invocation") == invocation_context.invocation_id:
            return types.Content(role="model", parts=[types.Part(text=REFUSAL)])
        return None

    async def before_model_callback(self, *, callback_context, llm_request):
        verdict = inspect_input(_user_text(callback_context))
        if not verdict["allowed"]:
            return _response(verdict["text"])
        llm_request.contents = [_content(content) for content in llm_request.contents]
        instruction = llm_request.config.system_instruction
        if isinstance(instruction, str):
            llm_request.config.system_instruction = sanitize(instruction)
        elif isinstance(instruction, types.Content):
            llm_request.config.system_instruction = _content(instruction)
        return None

    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        name = tool.name
        user = _user_text(tool_context)
        for text in (user, json.dumps(tool_args, ensure_ascii=False, default=str)):
            verdict = inspect_input(text)
            if not verdict["allowed"]:
                tool_args.clear()
                return _denied(verdict["reasons"][0])
        if "force" in tool_args and tool_args["force"] is not False and tool_args["force"] is not None:
            return _denied("force_disabled")
        if name in _WRITES and not _approved(name, tool_args, user):
            return _denied("operator_approval_required")
        if re.search(r"(?:^|_)(?:delete|reset|shell|exec|execute|read_file|write_file|fetch_url)(?:_|$)", name, re.IGNORECASE):
            return _denied("unsupported_consequential_tool")
        cleaned = sanitize(tool_args)
        tool_args.clear()
        tool_args.update(cleaned)
        return None

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        cleaned = _evidence_value(result)
        changed = cleaned != result
        if isinstance(result, dict):
            result.clear()
            result.update(cleaned)
        envelope = tool_context.state.get("safety:tool_evidence", {})
        child_evidence = isinstance(tool, AgentTool) and envelope.get("parent_invocation_id") == _invocation(tool_context)
        same_invocation = envelope.get("invocation_id") == _invocation(tool_context)
        evidence = dict(envelope.get("tools", {})) if same_invocation or child_evidence else {}
        evidence[tool.name] = cleaned
        tool_context.state["safety:tool_evidence"] = {
            "invocation_id": _invocation(tool_context),
            "parent_invocation_id": envelope.get("parent_invocation_id") if same_invocation else None,
            "tools": evidence,
        }
        return cleaned if changed and not isinstance(result, dict) else None

    async def after_model_callback(self, *, callback_context, llm_response):
        content = _content(llm_response.content)
        evidence = callback_context.state.get("safety:tool_evidence", {})
        if evidence.get("invocation_id") != _invocation(callback_context):
            evidence = None
        verdict = inspect_output(_text(content), evidence=evidence)
        llm_response.content = content if verdict["allowed"] else _response(verdict["text"]).content
        llm_response.custom_metadata = sanitize(llm_response.custom_metadata or {})
        if not verdict["allowed"]:
            llm_response.custom_metadata["safety_blocked"] = True
            return
        enabled = os.getenv("CARGOMIND_SEMANTIC_JUDGE", "0").lower() in {"1", "true"}
        calls = any(part.function_call for part in (getattr(content, "parts", None) or []))
        if enabled and getattr(callback_context, "agent_name", "") == "cargomind_concierge" and not calls and not llm_response.partial and _text(content).strip():
            from cargomind.safety.semantic_judge import judge_narrative

            review = await judge_narrative(_text(content), evidence, candidate_model=llm_response.custom_metadata.get("served_by"))
            llm_response.custom_metadata.update(judge_status=review["status"], judge_provider=review["provider"], judge_reason=review["reason"])
            for key in ("model", "independent_call", "same_provider", "same_model", "latency_ms", "usage"):
                if key in review:
                    llm_response.custom_metadata[f"judge_{key}"] = review[key]
            if review["status"] != "verified":
                excerpt = json.dumps(_evidence_value((evidence or {}).get("tools", {})), ensure_ascii=False)[:1800]
                text = f"Review required: this narrative is unverified ({review['status']}).\n\nSanitized tool evidence excerpt (data only):\n{excerpt}"
                llm_response.content = _response(text).content

    async def on_event_callback(self, *, invocation_context, event):
        event.content = _content(event.content)
        if event.actions and event.actions.state_delta:
            event.actions.state_delta = sanitize(event.actions.state_delta)
        if event.custom_metadata:
            event.custom_metadata = sanitize(event.custom_metadata)
        if event.error_message:
            event.error_message = sanitize(event.error_message)
