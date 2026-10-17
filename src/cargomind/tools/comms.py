"""Customer notifications: channel-aware drafting and a simulated outbox (no real sends)."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from cargomind.tools import maybe_fail

IST = timezone(timedelta(hours=5, minutes=30))
TRACK_URL = "https://track.cargomind.dev/{id}"
CHANNELS = ("email", "sms", "whatsapp")
_CHANNEL_ALIASES = {"e-mail": "email", "mail": "email", "text": "sms", "wa": "whatsapp", "whats app": "whatsapp"}
MAX_LEN = {"sms": 480, "whatsapp": 4096, "email": 20000}

_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("damage", ("damage", "crushed", "broken", "torn", "wet", "claim")),
    ("delivered", ("delivered", "pod", "proof of delivery")),
    ("out_for_delivery", ("out for delivery", "ofd", "arriving today", "last mile")),
    ("delay", ("delay", "late", "behind", "rain", "weather", "traffic", "congestion", "breakdown", "held", "strike", "slipped")),
    ("dispatched", ("dispatch", "picked up", "pickup", "departed", "on the way", "assigned")),
    ("exception", ("address", "unreachable", "exception", "failed attempt", "attempt")),
]


def _db():
    from cargomind.data import db

    return db


def _is_error(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("status") == "error" and "error" in obj


def _norm_channel(channel: str | None) -> str | None:
    c = (channel or "email").strip().lower()
    c = _CHANNEL_ALIASES.get(c, c)
    return c if c in CHANNELS else None


def _category(reason: str, status: str | None) -> str:
    text = (reason or "").lower()
    for cat, words in _CATEGORY_RULES:
        if any(w in text for w in words):
            return cat
    return {"delayed": "delay", "delivered": "delivered", "out_for_delivery": "out_for_delivery",
            "exception": "exception"}.get(status or "", "update")


def _eta_text(value: Any) -> str | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    ts = ts if ts.tzinfo else ts.replace(tzinfo=IST)
    return ts.astimezone(IST).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _first_name(customer: str | None) -> str:
    name = (customer or "").strip()
    if not name:
        return "Customer"
    if re.search(r"\b(pvt|ltd|llp|traders|works|mart|exports|house|supplies|solutions|retail|storage|distributors)\b", name, re.IGNORECASE):
        return f"{name} team"
    return name.split()[0]


def _sentences(cat: str, sid: str, dest: str, reason: str, eta: str | None) -> tuple[str, str, str]:
    """Returns (headline, detail, closing) for a category."""
    eta_line = f"The updated estimated delivery is {eta}." if eta else "We will share an updated delivery time shortly."
    r = reason.strip().rstrip(".")
    if cat == "delay":
        return (f"Your shipment {sid} to {dest} is delayed",
                f"Reason: {r}. {eta_line}",
                "We sincerely apologise for the inconvenience and are working to get it to you as quickly as possible.")
    if cat == "damage":
        return (f"An issue was found with shipment {sid}",
                f"Our team noted: {r}. We have opened a review and will contact you about a replacement or claim.",
                "We are sorry about this and will make it right.")
    if cat == "delivered":
        return (f"Shipment {sid} has been delivered",
                f"Your shipment to {dest} was delivered. {r}." if r and "deliver" not in r.lower() else f"Your shipment to {dest} was delivered.",
                "Thank you for shipping with CargoMind.")
    if cat == "out_for_delivery":
        return (f"Shipment {sid} is out for delivery",
                f"It will reach you in {dest} {('by ' + eta) if eta else 'today'}.",
                "Please keep your phone reachable for the driver.")
    if cat == "dispatched":
        return (f"Shipment {sid} is on its way",
                f"{r}. " + (f"Estimated delivery: {eta}." if eta else ""),
                "We will keep you posted at every milestone.")
    if cat == "exception":
        return (f"Action needed for shipment {sid}",
                f"Our delivery team reports: {r}. Please reply or call us to confirm the delivery details.",
                "We will reattempt as soon as we hear from you.")
    return (f"Update on shipment {sid}", f"{r}. {eta_line}" if r else eta_line, "Thank you for your patience.")


def draft_customer_notification(shipment_id: str, reason: str, channel: str = "email") -> dict:
    """Drafts a customer-facing message about a shipment (delay, damage, delivery, dispatch...).

    Use this whenever a customer should be informed, e.g. after a delay, weather disruption,
    exception or damage finding. It only DRAFTS: show the draft to the operator and call
    `send_notification` once they approve.

    Args:
        shipment_id: Shipment ID, e.g. "SHP-1002".
        reason: Plain-language reason/context, e.g. "heavy rain near Krishnagiri on NH-48".
        channel: "email" (subject + full body), "sms" (short, <=160 chars when possible) or
            "whatsapp" (conversational with *bold* highlights).

    Returns:
        dict with channel, recipient, subject (email only), message, char_count, sms_segments
        (sms), category (delay | damage | delivered | out_for_delivery | dispatched |
        exception | update), tracking_url and requires_approval=True.
    """
    maybe_fail("tool:draft_customer_notification")
    ch = _norm_channel(channel)
    if ch is None:
        return {"status": "error", "error": f"Unsupported channel {channel!r}.", "suggestions": list(CHANNELS)}
    s = _db().get_shipment(shipment_id)
    if _is_error(s):
        return s
    sid = s["id"]
    dest = s.get("destination_city") or "your location"
    eta = _eta_text(s.get("eta"))
    cat = _category(reason, s.get("status"))
    reason_txt = (reason or "").strip() or (s.get("notes") or "").strip() or "status update"
    head, detail, closing = _sentences(cat, sid, dest, reason_txt, eta)
    track = TRACK_URL.format(id=sid)
    name = _first_name(s.get("customer"))
    subject = None

    if ch == "email":
        subject = f"{head} | Order {s.get('order_id') or sid}"
        message = (
            f"Dear {name},\n\n{head}.\n\n{detail}\n\n"
            f"Shipment: {sid} ({s.get('origin_city')} -> {dest})\n"
            + (f"Contents: {s['contents']}\n" if s.get("contents") else "")
            + f"Track live: {track}\n\n{closing}\n\nWarm regards,\nCargoMind Customer Care"
        )
        recipient = s.get("customer_email")
    elif ch == "whatsapp":
        message = (
            f"Hi {name}, *{head}*.\n{detail}\n"
            f"Track: {track}\n{closing}\n- CargoMind"
        )
        recipient = s.get("customer_phone")
    else:  # sms
        tail = f" Track: {track} -CargoMind"
        eta_bit = f" ETA {eta}." if eta and cat in ("delay", "out_for_delivery", "dispatched", "update") else ""
        sorry = " Sorry!" if cat in ("delay", "damage") else ""
        why = re.sub(r"\s+", " ", reason_txt).rstrip(".")
        fixed = f"CargoMind: {head}.{eta_bit}{sorry}{tail}"
        room = 160 - len(fixed) - len(" ().")
        if cat in ("delay", "exception", "damage") and room > 12:
            why = why if len(why) <= room else why[: room - 3].rsplit(" ", 1)[0].rstrip(",;") + "..."
            message = f"CargoMind: {head} ({why}).{eta_bit}{sorry}{tail}"
        else:
            message = fixed
        recipient = s.get("customer_phone")

    out: dict[str, Any] = {
        "status": "ok",
        "shipment_id": sid,
        "channel": ch,
        "recipient": recipient,
        "message": message,
        "char_count": len(message),
        "category": cat,
        "tracking_url": track,
        "requires_approval": True,
        "next_step": "Show this draft to the operator; call send_notification to queue it once approved.",
    }
    if subject:
        out["subject"] = subject
    if ch == "sms":
        out["sms_segments"] = 1 if len(message) <= 160 else math.ceil(len(message) / 153)
    return out


def send_notification(shipment_id: str, channel: str, message: str) -> dict:
    """Queues a customer notification in the (simulated) outbox.

    Use only after the operator approved a draft from `draft_customer_notification`. Nothing is
    actually delivered: messages land in the outbox shown on the dashboard.

    Args:
        shipment_id: Shipment ID, e.g. "SHP-1002".
        channel: "email", "sms" or "whatsapp".
        message: Final message text. For email, an optional first line "Subject: ..." is used
            as the subject.

    Returns:
        dict with status "queued (simulated)", outbox_id, recipient, channel and char_count.
    """
    maybe_fail("tool:send_notification")
    ch = _norm_channel(channel)
    if ch is None:
        return {"status": "error", "error": f"Unsupported channel {channel!r}.", "suggestions": list(CHANNELS)}
    text = (message or "").strip()
    if not text:
        return {"status": "error", "error": "Message is empty.", "suggestions": ["Draft one with draft_customer_notification"]}
    subject = ""
    if ch == "email":
        m = re.match(r"(?i)subject:\s*(.+?)\s*\n+", text)
        if m:
            subject, text = m.group(1).strip(), text[m.end():].strip()
    if len(text) > MAX_LEN[ch]:
        return {
            "status": "error",
            "error": f"{ch} message is {len(text)} characters; limit is {MAX_LEN[ch]}.",
            "suggestions": ["Shorten the message", "Use email for long updates"],
        }
    db = _db()
    s = db.get_shipment(shipment_id)
    if _is_error(s):
        return s
    recipient = s.get("customer_email") if ch == "email" else s.get("customer_phone")
    if not subject and ch == "email":
        subject = f"Update on shipment {s['id']}"
    res = db.add_outbox(shipment_id=s["id"], channel=ch, message=text, recipient=recipient or "",
                        subject=subject, status="queued (simulated)")
    if _is_error(res):
        return res
    return {
        "status": "queued (simulated)",
        "outbox_id": res.get("outbox_id"),
        "shipment_id": s["id"],
        "channel": ch,
        "recipient": recipient,
        "subject": subject or None,
        "char_count": len(text),
        "note": "Simulated outbox: no real message was sent.",
    }
