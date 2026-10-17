"""Proof-of-delivery photo inspection.

Online: Gemini multimodal with a JSON response schema. Offline or on model failure: a Pillow
image-quality heuristic (sharpness, exposure, dominant colours) that is honest about what it
cannot judge; those results carry ``degraded=True``.
"""
from __future__ import annotations

import asyncio
import io
import json
import mimetypes
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from cargomind import config
from cargomind.tools import journal, maybe_fail, short_error

MAX_BYTES = 20 * 1024 * 1024
_FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif", "BMP": "image/bmp"}


class PodInspection(BaseModel):
    """Structured output requested from Gemini."""

    damage_detected: bool
    severity: Literal["none", "minor", "major"]
    findings: list[str] = Field(default_factory=list)
    package_count_visible: int = 0
    label_readable: bool = False
    recommended_action: Literal["accept", "accept_with_note", "open_damage_claim", "retake_photo"]
    confidence: float = 0.0


_PROMPT = """You are a proof-of-delivery (POD) inspector for an Indian logistics company.
Inspect the delivery photo{ref}. Report only what is visible:
- damage_detected / severity: crushed or dented corners, torn or punctured cartons, water stains
  or wet patches, broken or re-taped seals, spilled contents. "minor" = cosmetic, contents likely
  fine; "major" = contents likely affected or package opened.
- findings: short factual observations (max 6).
- package_count_visible: number of distinct packages visible.
- label_readable: whether a shipping label / AWB is legible.
- recommended_action: accept | accept_with_note | open_damage_claim | retake_photo
  (retake_photo if the image is too blurry/dark to judge).
- confidence: 0-1."""


def _resolve_image(image_ref: str) -> Path | dict:
    """Maps image_ref to a file inside UPLOAD_DIR, refusing path traversal."""
    ref = (image_ref or "").strip()
    if not ref:
        return {"status": "error", "error": "image_ref is empty.", "suggestions": _recent_uploads()}
    upload_dir = Path(config.UPLOAD_DIR).resolve()
    if ".." in Path(ref.replace("\\", "/")).parts:
        return {"status": "error", "error": "Invalid image_ref (path traversal is not allowed).", "suggestions": _recent_uploads()}
    candidates = [upload_dir / ref, upload_dir / Path(ref.replace("\\", "/")).name]
    for cand in candidates:
        try:
            path = cand.resolve()
        except OSError:
            continue
        if path.is_relative_to(upload_dir) and path.is_file():
            return path
    return {"status": "error", "error": f"No uploaded image named {ref!r}.", "suggestions": _recent_uploads()}


def _recent_uploads(limit: int = 5) -> list[str]:
    try:
        files = [p for p in Path(config.UPLOAD_DIR).iterdir() if p.is_file()]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in files[:limit]]


async def _gemini_inspect(data: bytes, mime: str, shipment_id: str) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client()
    prompt = _PROMPT.format(ref=f" for shipment {shipment_id}" if shipment_id else "")
    resp = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=config.GEMINI_VISION,
            contents=[types.Part.from_bytes(data=data, mime_type=mime), prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PodInspection,
                temperature=0.1,
            ),
        ),
        timeout=max(config.HTTP_TIMEOUT_S * 3, 20.0),
    )
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, PodInspection):
        out = parsed.model_dump()
    else:
        out = PodInspection.model_validate(json.loads(resp.text or "{}")).model_dump()
    out["confidence"] = round(max(0.0, min(1.0, float(out.get("confidence") or 0.0))), 2)
    return out


def _heuristic(img_bytes: bytes) -> dict[str, Any]:
    from PIL import Image, ImageFilter, ImageStat

    with Image.open(io.BytesIO(img_bytes)) as im:
        im.load()
        width, height = im.size
        rgb = im.convert("RGB")
    rgb.thumbnail((512, 512))
    gray = rgb.convert("L")
    brightness = ImageStat.Stat(gray).mean[0]
    edge_var = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]
    quant = rgb.quantize(colors=5)
    palette = quant.getpalette() or []
    counts = sorted(quant.getcolors() or [], reverse=True)
    total = sum(c for c, _ in counts) or 1
    dominant = []
    cardboard_share = 0.0
    for count, idx in counts:
        r, g, b = palette[idx * 3 : idx * 3 + 3]
        share = count / total
        dominant.append({"hex": f"#{r:02x}{g:02x}{b:02x}", "share_pct": round(100 * share, 1)})
        # Kraft/cardboard brown: warm, moderately saturated, mid brightness.
        if r > g > b and 70 <= r <= 230 and (r - b) >= 35 and (r - g) <= 70:
            cardboard_share += share

    findings: list[str] = []
    quality_ok = True
    if edge_var < 60:
        findings.append(f"Image is very blurry (edge variance {edge_var:.0f}); damage cannot be judged.")
        quality_ok = False
    elif edge_var < 150:
        findings.append(f"Image is slightly soft (edge variance {edge_var:.0f}).")
    if brightness < 50:
        findings.append(f"Image is too dark (mean brightness {brightness:.0f}/255).")
        quality_ok = False
    elif brightness > 230:
        findings.append(f"Image is over-exposed (mean brightness {brightness:.0f}/255).")
        quality_ok = False
    if min(width, height) < 320:
        findings.append(f"Low resolution ({width}x{height}).")
    if cardboard_share >= 0.15:
        findings.append(f"Cardboard-coloured surfaces cover ~{100 * cardboard_share:.0f}% of the frame (package likely in view).")
    else:
        findings.append("No dominant cardboard colour detected; package may not be in frame.")
    return {
        "quality_ok": quality_ok,
        "findings": findings,
        "image": {
            "width": width,
            "height": height,
            "brightness": round(brightness, 1),
            "sharpness_edge_variance": round(edge_var, 1),
            "dominant_colors": dominant,
        },
    }


async def inspect_delivery_photo(image_ref: str, shipment_id: str = "") -> dict:
    """Inspects an uploaded proof-of-delivery photo for package damage.

    Use this when the user uploads a delivery/POD photo or asks whether a delivered package
    was damaged. The image must already be uploaded; pass its file name.

    Args:
        image_ref: File name of the uploaded image (e.g. "pod_SHP-1004.jpg").
        shipment_id: Optional shipment ID the photo belongs to, e.g. "SHP-1004".

    Returns:
        dict with damage_detected (True/False, or None when the offline heuristic cannot
        judge), severity (none | minor | major | unknown), findings[], package_count_visible,
        label_readable, recommended_action (accept | accept_with_note | open_damage_claim |
        retake_photo | manual_review), confidence, source (gemini | pillow_heuristic) and
        degraded. If major damage is found, suggest opening a claim and notifying the customer.
    """
    maybe_fail("tool:inspect_delivery_photo")
    resolved = _resolve_image(image_ref)
    if isinstance(resolved, dict):
        return resolved
    path = resolved
    size = path.stat().st_size
    if size > MAX_BYTES:
        return {"status": "error", "error": f"Image is {size / 1e6:.1f} MB; limit is {MAX_BYTES // 1_000_000} MB.", "suggestions": ["Upload a smaller photo"]}
    data = path.read_bytes()
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format or ""
    except Exception:  # noqa: BLE001 - not an image Pillow understands
        return {"status": "error", "error": f"{path.name} is not a readable image.", "suggestions": ["Upload a JPEG or PNG photo"]}
    mime = _FORMAT_MIME.get(fmt) or mimetypes.guess_type(path.name)[0] or "image/jpeg"

    base = {"status": "ok", "image_ref": path.name, "shipment_id": shipment_id or None}
    chain: list[str] = []
    fallback_reason = "offline mode" if not config.online_gemini() else ""
    if config.online_gemini():
        try:
            result = await _gemini_inspect(data, mime, shipment_id)
            chain.append(f"gemini ({config.GEMINI_VISION}): ok")
            return {**base, **result, "source": "gemini", "model": config.GEMINI_VISION, "degraded": False, "provider_chain_tried": chain}
        except Exception as exc:  # noqa: BLE001 - fall back to the local heuristic
            fallback_reason = short_error(exc)
            chain.append(f"gemini ({config.GEMINI_VISION}): failed ({fallback_reason})")
            journal("fallback", "vision.gemini", fallback_reason, action="pillow heuristic")
    else:
        chain.append("gemini: skipped (offline mode or no API key)")

    h = _heuristic(data)
    chain.append("pillow_heuristic: ok")
    action = "manual_review" if h["quality_ok"] else "retake_photo"
    return {
        **base,
        "damage_detected": None,
        "severity": "unknown",
        "findings": h["findings"],
        "package_count_visible": None,
        "label_readable": None,
        "recommended_action": action,
        "confidence": 0.2 if h["quality_ok"] else 0.5,
        "image": h["image"],
        "source": "pillow_heuristic",
        "degraded": True,
        "provider_chain_tried": chain,
        "note": (
            f"Vision model unavailable ({fallback_reason}). Only image quality was checked locally; "
            "damage was NOT assessed. A human should review this photo."
            if h["quality_ok"]
            else f"Vision model unavailable ({fallback_reason}) and the photo quality is too poor to judge; ask for a retake."
        ),
    }
