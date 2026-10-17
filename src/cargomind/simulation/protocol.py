"""Custom versioned JSON messages exchanged between deterministic agent roles.

The JSON encode/decode boundary validates every received message, evidence hash,
role transition, task/parent correlation and bounded hop/time budget.

Hashes provide integrity, not authentication or remote-agent identity. The RPC
coordinates registered trusted roles; it does not accept arbitrary remote handoffs.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import Field

from cargomind.simulation.models import PROTOCOL_VERSION, StrictModel, canonical_json

Role = Literal["operator", "constraint_agent", "optimizer_agent", "evaluator_agent", "output_gate"]
MessageStatus = Literal["requested", "validated", "solved", "evaluated", "approved", "no_route", "rejected"]
ALLOWED_ROUTES = {
    ("operator", "constraint_agent"),
    ("constraint_agent", "optimizer_agent"),
    ("constraint_agent", "output_gate"),
    ("optimizer_agent", "evaluator_agent"),
    ("evaluator_agent", "output_gate"),
    ("output_gate", "operator"),
}
ALLOWED_STATUSES = {
    ("operator", "constraint_agent"): {"requested"},
    ("constraint_agent", "optimizer_agent"): {"validated"},
    ("constraint_agent", "output_gate"): {"rejected"},
    ("optimizer_agent", "evaluator_agent"): {"solved"},
    ("evaluator_agent", "output_gate"): {"evaluated"},
    ("output_gate", "operator"): {"approved", "no_route", "rejected"},
}


class ProtocolError(ValueError):
    pass


class Handoff(StrictModel):
    sender: Role
    recipient: Role
    task_id: str = Field(pattern=r"^sim-[0-9a-f]{24}$")
    message_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: MessageStatus
    hop: int = Field(ge=0, le=8)
    payload: dict[str, Any]


class AgentReply(StrictModel):
    recipient: Role
    status: MessageStatus
    payload: dict[str, Any]


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class ProtocolSession:
    def __init__(self, task_id: str, *, max_hops: int = 8, timeout_seconds: float = 5.0):
        self.task_id = task_id
        self.max_hops = min(max_hops, 8)
        self.timeout_seconds = timeout_seconds
        self.handlers: dict[str, Callable[[Handoff], AgentReply]] = {}
        self.handoffs: list[dict] = []
        self._received_ids: set[str] = set()

    def register(self, role: Role, handler: Callable[[Handoff], AgentReply]) -> None:
        if role not in {"constraint_agent", "optimizer_agent", "evaluator_agent", "output_gate"} or role in self.handlers:
            raise ProtocolError("Agent role cannot be registered twice or replace the operator")
        self.handlers[role] = handler

    def _receive(self, wire: str, *, previous: Handoff | None, expected_hop: int) -> Handoff:
        if len(wire.encode("utf-8")) > 262_144:
            raise ProtocolError("Message exceeds the bounded JSON payload size")
        message = Handoff.model_validate_json(wire)
        if (message.sender, message.recipient) not in ALLOWED_ROUTES:
            raise ProtocolError("Role transition is not allowed")
        if message.status not in ALLOWED_STATUSES[(message.sender, message.recipient)]:
            raise ProtocolError("Message status is not allowed for this role transition")
        if previous is None and (message.sender != "operator" or message.recipient != "constraint_agent"
                                 or message.status != "requested" or message.hop != 0):
            raise ProtocolError("Initial handoff must request constraint validation from the operator")
        if message.task_id != self.task_id or message.hop != expected_hop:
            raise ProtocolError("Message task or hop does not match this exchange")
        if message.hop >= self.max_hops:
            raise ProtocolError("Agent exchange exceeded its hop budget")
        if message.parent_id != (previous.message_id if previous else None):
            raise ProtocolError("Message parent does not match the previous handoff")
        if previous and message.sender != previous.recipient:
            raise ProtocolError("Message sender does not match the receiving agent")
        if previous and (previous.task_id != self.task_id or message.hop != previous.hop + 1):
            raise ProtocolError("Message does not continue this task's previous hop")
        if message.evidence_hash != payload_hash(message.payload):
            raise ProtocolError("Message payload evidence hash is invalid")
        metadata = message.model_dump(mode="json", exclude={"message_id"})
        if message.message_id != payload_hash(metadata):
            raise ProtocolError("Message identifier does not match its contents")
        if message.message_id in self._received_ids:
            raise ProtocolError("Message replay within this exchange is not allowed")
        self._received_ids.add(message.message_id)
        return message

    def run(self, initial_payload: dict) -> dict:
        started = time.monotonic()
        sender: Role = "operator"
        reply = AgentReply(recipient="constraint_agent", status="requested", payload=initial_payload)
        previous = None
        for hop in range(self.max_hops):
            if time.monotonic() - started >= self.timeout_seconds:
                raise ProtocolError("Agent exchange exceeded its time budget")
            metadata = {
                "sender": sender, "recipient": reply.recipient, "task_id": self.task_id,
                "parent_id": previous.message_id if previous else None,
                "protocol_version": PROTOCOL_VERSION, "evidence_hash": payload_hash(reply.payload),
                "status": reply.status, "hop": hop, "payload": reply.payload,
            }
            wire = canonical_json({**metadata, "message_id": payload_hash(metadata)})
            received = self._receive(wire, previous=previous, expected_hop=hop)
            self.handoffs.append(received.model_dump(mode="json", exclude={"payload"}))
            if received.recipient == "operator":
                return received.payload
            handler = self.handlers.get(received.recipient)
            if handler is None:
                raise ProtocolError("Receiving agent is unavailable")
            reply = AgentReply.model_validate(handler(received))
            if time.monotonic() - started >= self.timeout_seconds:
                raise ProtocolError("Agent exchange exceeded its time budget")
            sender, previous = received.recipient, received
        raise ProtocolError("Agent exchange exceeded its hop budget")
