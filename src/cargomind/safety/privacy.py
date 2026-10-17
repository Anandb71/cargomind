"""Pure, network-free privacy checks. Rejected requests are never echoed back."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

REFUSAL = (
    "I can help with shipment IDs, city-level operations and reviewed simulation actions. "
    "I cannot expose personal data or credentials, export private records, or bypass safeguards."
)
_REDACTED = "[redacted]"
_EMAIL = re.compile(r"(?<![\w.-])[\w.!#$%&'*+/=?^`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<![\w-])(?:\+\d(?:[ ()-]?\d){9,14}|[6-9]\d{9})(?![\w-])")
_UNITS = re.compile(r"^\s*(?:kg|kgs|km|kms|units|pieces|pallets|inr|rupees|tonnes|hours|minutes|%)(?:\b|$)", re.IGNORECASE)
_KEY = re.compile(
    r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{25,}|gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:[A-Z_]*(?:API[_ -]?KEY|PASSWORD|SECRET|ACCESS[_ -]?TOKEN|AUTHORIZATION))[\"']?\s*[:=]\s*[^\n,;}]+"
)
_QUOTED_SECRET = re.compile(
    r"(?i)\b(?:[A-Z_]*(?:API[_ -]?KEY|PASSWORD|SECRET|ACCESS[_ -]?TOKEN|AUTHORIZATION))[\"']?\s*[:=]\s*"
    r"(?P<quote>[\"'])(?:\\[\s\S]|(?!(?P=quote))[\s\S]){0,4096}(?P=quote)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
_LABELED_NAME = re.compile(
    r"(?i)(\b(?:customer(?:[_ ]name)?|driver(?:[_ ]name)?|manager|full[_ ]name|personal[_ ]name|recipient[_ ]name)"
    r"[\"']?\s*[:=]\s*[\"']?)(?!Customer-|Driver-|Manager-|\[redacted\])([^\n,;|\"'}]+)"
)
_INTRO_NAME = re.compile(r"(?i)(\b(?:my name is|customer is|driver is|manager is|dear|hello)\s+)([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){0,3})(?=[,\n.!]|$)")
_STREET = re.compile(r"\b\d{1,5}[A-Za-z]?\s+(?:[A-Za-z][\w.'-]*\s+){0,5}(?:Road|Street|Lane|Avenue|Drive|Boulevard|Rd|St)\b(?:[^\n;|]{0,50})", re.IGNORECASE)
_REGISTRATION = re.compile(r"\b[A-Z]{2}[- ]?\d{1,2}[- ]?[A-Z]{1,3}[- ]?\d{4}\b")
_IDENTITY_LABEL = re.compile(r"(?i)\b(?:aadhaar|aadhar|passport|ssn|national[_ ]?id)\s*[:=#]?\s*[A-Z0-9 -]{6,20}")

_DROP_KEYS = {
    "email", "email_address", "customer_email", "driver_email", "contact_email", "recipient_email",
    "phone", "phone_number", "mobile", "mobile_number", "customer_phone", "driver_phone", "contact_phone",
    "address", "street", "street_address", "home_address", "customer_address", "delivery_address",
    "recipient", "registration", "registration_number", "license_plate", "driver_license",
    "ssn", "aadhaar", "aadhar", "passport", "date_of_birth", "dob", "personal_name", "full_name",
    "first_name", "last_name", "contact", "contact_details", "contacts",
}
_ROLE_KEYS = {"customer": "Customer", "customer_name": "Customer", "client_name": "Customer", "consignee_name": "Recipient", "contact_name": "Person", "driver": "Driver", "driver_name": "Driver", "manager": "Manager", "manager_name": "Manager", "recipient_name": "Recipient", "person_name": "Person"}
_SECRET_KEYS = re.compile(r"(?:^|_)(?:api_key|apikey|password|secret|token|credential|credentials|authorization|cookie|private_key)(?:$|_)", re.IGNORECASE)
_PERSONAL_KEYS = re.compile(r"(?:^|_)(?:email|phone|mobile|address|passport|aadhaar|aadhar|ssn)(?:$|_)", re.IGNORECASE)
_OPERATIONAL_ALIAS = re.compile(r"^(?:Customer|Driver|Manager|Recipient|Person)(?:-[A-Za-z0-9_-]+)?$")
_PROTOCOL_RECIPIENTS = {"operator", "constraint_agent", "optimizer_agent", "evaluator_agent", "output_gate"}
_SERVICE_NAME = re.compile(r"^(?:CargoMind(?:[ _-]?Simulation)?|cargomind[_-][a-z0-9_-]+|(?:constraint|optimizer|evaluator)_agent|output_gate)$", re.IGNORECASE)

_READ_VERB = r"(?:show|give|list|reveal|print|dump|find|tell|what(?:'s| is| are)?|get|read|return|extract|display)"
_PII_REQUEST = re.compile(
    rf"\b{_READ_VERB}\b[\s\S]{{0,120}}\b(?:(?:customer|driver|recipient|manager|personal)(?:'s|s)?\s+(?:names?|contact(?:s| details)?|emails?|phones?|address(?:es)?)|(?:phone|mobile) numbers?|home address(?:es)?|PII|passport|aadhaar|ssn)\b", re.IGNORECASE
)
_CREDENTIAL_REQUEST = re.compile(rf"\b{_READ_VERB}\b[\s\S]{{0,120}}(?:\b(?:api[_ -]?keys?|passwords?|secrets?|credentials?|access[_ -]?tokens?|environment variables?)\b|\.env\b)", re.IGNORECASE)
_OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|forget|replace)\s+(?:(?:all|any|the|your|previous|prior|above|system|developer|safety)\s+){0,5}(?:instructions?|rules?|prompts?|polic(?:y|ies)|safeguards?)\b"
    r"|\b(?:bypass|disable|override)\s+(?:(?:all|the|your|safety|security|approval|privacy|validation)\s+){0,4}(?:checks?|filters?|safeguards?|rules?|guardrails?|restrictions?|validation|approval)\b"
    r"|(?:<\|?(?:system|developer)\|?>|\[INST\]|\[SYSTEM\])"
    r"|\b(?:reveal|print|show|repeat)\b.{0,40}\b(?:system|developer)\s+prompt\b", re.IGNORECASE
)
_EXPORT = re.compile(r"\b(?:exfiltrate|export|upload|transmit|dump)\b.{0,120}\b(?:database|records|contacts|credentials|private|secrets|webhook)\b|\b(?:send|post|forward|copy)\b.{0,120}\b(?:all records|entire database|credentials|api[_ -]?keys?|passwords?|private data)\b|\b(?:send|post|upload|forward|export)\b.{0,120}https?://", re.IGNORECASE | re.DOTALL)


def _has_override(text: str) -> bool:
    return any(not re.search(r"\b(?:do not|don't|never)\s*$", text[:match.start()], re.IGNORECASE) for match in _OVERRIDE.finditer(text))


def _key_name(key: Any) -> str:
    words = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(key)).lower()
    return re.sub(r"[^a-z0-9]+", "_", words).strip("_")


def _private_field(key: str) -> bool:
    if key in {"token_count", "input_token_count", "output_token_count", "total_token_count", "prompt_tokens", "completion_tokens"}:
        return False
    return key in _DROP_KEYS or bool(_SECRET_KEYS.search(key) or _PERSONAL_KEYS.search(key))


def _scrub_text(text: str) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")
    # Nested MCP content may be JSON text rather than a structured dictionary.
    if text.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                cleaned = sanitize(parsed)
                return json.dumps(cleaned, ensure_ascii=False) if cleaned != parsed else text
        except (ValueError, TypeError):
            pass
    text = _PRIVATE_KEY.sub(_REDACTED, text)
    for pattern in (_KEY, _QUOTED_SECRET, _SECRET_ASSIGNMENT, _BEARER, _EMAIL, _STREET, _REGISTRATION, _IDENTITY_LABEL):
        text = pattern.sub(_REDACTED, text)
    text = _PHONE.sub(lambda m: m.group() if _UNITS.match(text[m.end():]) else _REDACTED, text)
    text = _LABELED_NAME.sub(lambda m: m.group(1) + _REDACTED, text)
    text = _INTRO_NAME.sub(lambda m: m.group(1) + _REDACTED, text)
    # Preserve table structure, but remove explicitly identified personal columns.
    columns: list[int] = []
    lines = []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("|"):
            cells = line.split("|")
            private = [i for i, cell in enumerate(cells) if _key_name(cell.strip(" *`")) in _ROLE_KEYS or _private_field(_key_name(cell.strip(" *`")))]
            if private:
                columns = private
            elif columns and not all(re.fullmatch(r"[\s:|-]*", cell) for cell in cells):
                for i in columns:
                    if i < len(cells) - 1:
                        cells[i] = f" {_REDACTED} "
                line = "|".join(cells)
        else:
            columns = []
        lines.append(line)
    return "".join(lines)


def sanitize(value: Any) -> Any:
    """Return a cleaned copy, retaining IDs, cities and numeric operational constraints.

    Explicit personal fields are removed or replaced by role aliases. Free text
    covers common credential/contact patterns and labeled identities, not general NER.
    """
    if isinstance(value, dict):
        out = {}
        operational = any(k in value for k in ("sku", "warehouse_id", "category", "capacity_kg")) or str(value.get("id", "")).startswith(("WH-", "SKU-", "TRK-", "CR-"))
        for key, item in value.items():
            # Identifiers can appear as dictionary keys too (and later become
            # JSON-pointer references). Never forward contact or secret keys.
            if isinstance(key, str) and _scrub_text(key) != key:
                continue
            name = _key_name(key)
            if name == "recipient" and isinstance(item, str) and item in _PROTOCOL_RECIPIENTS:
                out[key] = item
                continue
            if _private_field(name):
                continue
            if name in _ROLE_KEYS and isinstance(item, str):
                out[key] = item if _OPERATIONAL_ALIAS.fullmatch(item) else _ROLE_KEYS[name]
            elif name == "name" and not operational and not any(k in value for k in ("lat", "lon", "risk_level", "capabilities", "skills", "agent_id", "agent_name", "protocol")) and not str(item).startswith(("tool:", "model:", "mcp:")) and not _SERVICE_NAME.fullmatch(str(item)):
                out[key] = "Person" if isinstance(item, str) else sanitize(item)
            else:
                out[key] = sanitize(item)
        return out
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item) for item in value)
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def inspect_input(text: str) -> dict[str, Any]:
    """Reject explicit unsafe requests and supplied sensitive data before any tool/model call."""
    text = str(text or "")
    checked = "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")
    reasons = []
    for pattern, reason in ((_PII_REQUEST, "personal_data_request"), (_CREDENTIAL_REQUEST, "credential_request"), (_EXPORT, "data_exfiltration")):
        if pattern.search(checked):
            reasons.append(reason)
    if _has_override(checked):
        reasons.append("instruction_override")
    cleaned = sanitize(text)
    if cleaned != text:
        reasons.append("sensitive_input")
    return {"allowed": not reasons, "text": REFUSAL if reasons else cleaned, "reasons": reasons}


def inspect_output(text: str, evidence: Any = None) -> dict[str, Any]:
    """Sanitize output and check action claims against caller-provided current-turn evidence.

    Delivery status facts from tracking are not execution claims. A simulated
    notification is only queued, and reroutes remain proposals even after approval.
    The caller must scope evidence to this invocation; the ADK plugin does so.
    """
    text = str(text or "")
    cleaned = sanitize(text)
    reasons = ["sensitive_output_redacted"] if cleaned != text else []
    if _has_override(cleaned):
        return {"allowed": False, "text": REFUSAL, "reasons": [*reasons, "instruction_override"]}
    tools = evidence.get("tools", evidence) if isinstance(evidence, dict) else {}
    notification = tools.get("send_notification", {})
    commit = tools.get("commit_dispatch_plan", {})
    proposal = tools.get("propose_dispatch_plan", {})
    queued = isinstance(notification, dict) and notification.get("status") == "queued (simulated)" and bool(notification.get("outbox_id"))
    committed = isinstance(commit, dict) and commit.get("status") in ("ok", "partial") and bool(commit.get("applied"))
    action = re.compile(
        r"\b(?P<verb>rerouted|re-routed|reassigned|dispatched|emailed|texted|sent|queued|committed)\b"
        r"|\b(?:notification|message|email|sms|whatsapp)(?:s)?\b.{0,45}\b(?P<delivery>delivered)\b", re.IGNORECASE
    )
    for clause in re.split(r"[.;\n]|\bbut\b", cleaned):
        for match in action.finditer(clause):
            verb = (match.group("verb") or match.group("delivery")).lower()
            prefix = clause[:match.start(match.lastgroup)].lower()
            if re.search(r"\b(?:not|never|cannot|can't)(?:\s+\w+){0,2}\s*$", prefix) or re.search(r"\bno\s+(?:real\s+|actual\s+)?(?:notifications?|messages?|emails?|sms|whatsapp)(?:\s+\w+){0,3}\s*$", prefix):
                continue
            if re.search(r"\b(?:propos(?:al|als|ed)|draft|would|could|will|planned|pending|once approved|if approved)\b", clause, re.IGNORECASE):
                continue
            simulated = bool(re.search(r"\b(?:simulated|simulation|outbox)\b", clause, re.IGNORECASE))
            if verb == "queued" and isinstance(proposal, dict) and proposal.get("plan_status") == "proposed" and re.search(r"^\s+for\s+(?:the\s+)?next\s+wave\b", clause[match.end():], re.IGNORECASE):
                continue  # A deferred load in a proposal is not a notification/outbox mutation.
            if verb == "queued" and queued:
                continue
            if verb == "committed" and committed:
                continue
            if verb == "reassigned" and committed and simulated:
                continue
            if simulated and verb in ("sent", "queued", "emailed", "texted") and queued:
                continue
            # "sent: false" is a tool field, not a claim that a notice was sent.
            if re.match(r"[\s*`:_=-]*(?:false|no)\b", clause[match.end():], re.IGNORECASE):
                continue
            if verb == "sent" and not re.search(r"\b(?:notification|notice|message|email|sms|whatsapp|I|we|customer)\b", clause, re.IGNORECASE):
                continue
            return {"allowed": False, "text": "Review the tool results: reroutes are proposals and notifications use a simulated outbox; no real-world execution is established.", "reasons": [*reasons, "unsupported_execution_claim"]}
    return {"allowed": True, "text": cleaned, "reasons": reasons}
