"""KYC & onboarding tools: OCR, document verification, screening, risk and reporting.

Identity-document logic uses the published algorithms (ICAO 9303 MRZ check digits,
Verhoeff for Aadhaar, ITD format rules for PAN) rather than approximations.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
import uuid
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.errors import NotFoundError, ProviderNotConfiguredError, ValidationError
from app.core.storage import store
from app.db.models.banking import KycCase, KycDocument, SanctionsEntry
from app.llm.router import router
from app.tools.base import ToolContext, tool

# --- checksum algorithms ------------------------------------------------------
VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_valid(number: str) -> bool:
    digits = re.sub(r"\D", "", number)
    if len(digits) != 12:
        return False
    c = 0
    for i, digit in enumerate(reversed(digits)):
        c = VERHOEFF_D[c][VERHOEFF_P[i % 8][int(digit)]]
    return c == 0


MRZ_WEIGHTS = [7, 3, 1]


def mrz_check_digit(value: str) -> int:
    total = 0
    for i, char in enumerate(value):
        if char.isdigit():
            v = int(char)
        elif char.isalpha():
            v = ord(char.upper()) - 55
        else:
            v = 0
        total += v * MRZ_WEIGHTS[i % 3]
    return total % 10


PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PAN_HOLDER_TYPES = {
    "P": "Individual", "C": "Company", "H": "HUF", "F": "Firm", "A": "AOP",
    "T": "Trust", "B": "Body of Individuals", "L": "Local Authority",
    "J": "Artificial Juridical Person", "G": "Government",
}


def normalise_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name or "")
    ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z ]", " ", ascii_name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def name_similarity(a: str, b: str) -> float:
    na, nb = normalise_name(a), normalise_name(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    tokens_a, tokens_b = set(na.split()), set(nb.split())
    jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if tokens_a | tokens_b else 0.0
    return round(0.6 * seq + 0.4 * jaccard, 4)


# --- OCR ----------------------------------------------------------------------
class OcrArgs(BaseModel):
    case_number: str = Field(description="KYC case number the document belongs to")
    document_id: str | None = Field(default=None, description="Existing document id")
    doc_type: str | None = Field(default=None, description="passport|pan|aadhaar|selfie|utility_bill")


async def _load_document(ctx: ToolContext, case_number: str, document_id: str | None,
                         doc_type: str | None) -> tuple[KycCase, KycDocument]:
    case = (
        await ctx.session.execute(select(KycCase).where(KycCase.case_number == case_number))
    ).scalar_one_or_none()
    if case is None:
        raise NotFoundError(f"KYC case '{case_number}' not found")
    stmt = select(KycDocument).where(KycDocument.case_id == case.id)
    if document_id:
        stmt = stmt.where(KycDocument.id == document_id)
    if doc_type:
        stmt = stmt.where(KycDocument.doc_type == doc_type)
    doc = (await ctx.session.execute(stmt)).scalars().first()
    if doc is None:
        raise NotFoundError("No matching document uploaded for this case",
                            details={"case": case_number, "doc_type": doc_type})
    return case, doc


async def _ocr_bytes(data: bytes, mime_type: str) -> tuple[str, str]:
    """Returns (text, engine). Tesseract first, then a vision LLM."""
    try:
        import io

        import pytesseract
        from PIL import Image

        if mime_type.startswith("image/"):
            text = pytesseract.image_to_string(Image.open(io.BytesIO(data)))
            if text.strip():
                return text, "tesseract"
    except Exception:
        pass

    if mime_type == "application/pdf":
        try:
            import io

            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(io.BytesIO(data))
            pages = [pdf[i].get_textpage().get_text_range() for i in range(min(len(pdf), 5))]
            text = "\n".join(pages)
            if text.strip():
                return text, "pypdfium2"
        except Exception:
            pass

    vision = [m for m in router.available_models(embeddings=False) if m.supports_vision]
    if not vision:
        raise ProviderNotConfiguredError(
            "No OCR engine available: install Tesseract (pytesseract) or configure a "
            "vision-capable model provider",
            details={"tried": ["tesseract", "pypdfium2", "vision_llm"]},
        )
    b64 = base64.b64encode(data).decode()
    spec = vision[0]
    provider = router.provider(spec.provider)
    if spec.provider == "anthropic":
        payload = {
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type,
                                             "data": b64}},
                {"type": "text", "text": "Transcribe all text in this identity document verbatim, "
                                         "including any machine readable zone."},
            ]}],
            "max_tokens": 2000, "model": spec.id,
        }
        from app.core.config import settings as _s
        from app.llm.base import http_client

        resp = await http_client().post(
            f"{_s.anthropic_base_url}/v1/messages",
            headers={"x-api-key": _s.anthropic_api_key or "", "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload,
        )
        if resp.status_code >= 400:
            raise ProviderNotConfiguredError(f"Vision OCR failed: {resp.text[:400]}")
        blocks = resp.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text"), \
            f"vision:{spec.id}"

    # OpenAI-compatible vision
    from app.llm.base import http_client

    body = {
        "model": spec.id,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Transcribe all text in this identity document verbatim, "
                                     "including any machine readable zone."},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
        ]}],
        "max_tokens": 2000,
    }
    headers = {"Content-Type": "application/json"}
    if getattr(provider, "api_key", None):
        headers["Authorization"] = f"Bearer {provider.api_key}"  # type: ignore[union-attr]
    resp = await http_client().post(f"{provider.base_url}/chat/completions",  # type: ignore[union-attr]
                                    headers=headers, json=body)
    if resp.status_code >= 400:
        raise ProviderNotConfiguredError(f"Vision OCR failed: {resp.text[:400]}")
    return resp.json()["choices"][0]["message"]["content"], f"vision:{spec.id}"


@tool(
    "ocr_document",
    "Run OCR over an uploaded KYC document and return the extracted raw text.",
    OcrArgs,
    category="kyc",
    writes_data=True,
    timeout_seconds=90,
)
async def ocr_document(args: OcrArgs, ctx: ToolContext) -> dict[str, Any]:
    _, doc = await _load_document(ctx, args.case_number, args.document_id, args.doc_type)
    if doc.ocr_text:
        return {"document_id": doc.id, "doc_type": doc.doc_type, "engine": doc.ocr_engine,
                "characters": len(doc.ocr_text), "text": doc.ocr_text[:6000], "cached": True}
    if not doc.artifact_uri:
        raise ValidationError("Document has no stored artifact to OCR")
    key = doc.artifact_uri.split("/", 3)[-1] if "://" in doc.artifact_uri else doc.artifact_uri
    data = await store.get(key)
    text, engine = await _ocr_bytes(data, doc.mime_type)
    doc.ocr_text = text
    doc.ocr_engine = engine
    await ctx.session.flush()
    return {"document_id": doc.id, "doc_type": doc.doc_type, "engine": engine,
            "characters": len(text), "text": text[:6000], "cached": False}


class ClassifyArgs(BaseModel):
    case_number: str
    document_id: str | None = None


DOC_SIGNATURES = {
    "passport": [r"\bP<", r"passport", r"republic of", r"\bMRZ\b"],
    "pan": [r"income tax department", r"permanent account number", PAN_RE.pattern.strip("^$")],
    "aadhaar": [r"aadhaar", r"unique identification", r"\b\d{4}\s\d{4}\s\d{4}\b", r"\bUIDAI\b"],
    "utility_bill": [r"electricity", r"utility", r"bill", r"consumer number", r"billing period"],
    "bank_statement": [r"statement of account", r"ifsc", r"opening balance"],
    "driving_licence": [r"driving licence", r"driving license", r"\bDL No\b"],
}


@tool(
    "classify_document",
    "Classify an uploaded document type from its OCR text using signature matching.",
    ClassifyArgs,
    category="kyc",
    writes_data=True,
)
async def classify_document(args: ClassifyArgs, ctx: ToolContext) -> dict[str, Any]:
    _, doc = await _load_document(ctx, args.case_number, args.document_id, None)
    text = (doc.ocr_text or "").lower()
    if not text:
        raise ValidationError("Run ocr_document before classification")
    scores: dict[str, float] = {}
    for doc_type, patterns in DOC_SIGNATURES.items():
        hits = sum(1 for p in patterns if re.search(p, text, re.I))
        scores[doc_type] = hits / len(patterns)
    best = max(scores, key=lambda k: scores[k])
    confidence = scores[best]
    doc.classification_confidence = confidence
    if confidence >= 0.34:
        doc.doc_type = best
    await ctx.session.flush()
    return {"document_id": doc.id, "predicted_type": best, "confidence": round(confidence, 3),
            "scores": {k: round(v, 3) for k, v in scores.items()},
            "applied": confidence >= 0.34}


# --- document verification -----------------------------------------------------
class VerifyPassportArgs(BaseModel):
    case_number: str
    document_id: str | None = None


@tool(
    "verify_passport",
    "Parse and validate a passport MRZ (ICAO 9303): check digits, expiry, holder name and number.",
    VerifyPassportArgs,
    category="kyc",
    writes_data=True,
)
async def verify_passport(args: VerifyPassportArgs, ctx: ToolContext) -> dict[str, Any]:
    case, doc = await _load_document(ctx, args.case_number, args.document_id, "passport")
    text = doc.ocr_text or ""
    if not text:
        raise ValidationError("Run ocr_document before verification")
    lines = [re.sub(r"\s", "", line) for line in text.splitlines() if line.strip()]
    mrz = [ln for ln in lines if len(ln) >= 40 and ln.count("<") >= 3]
    findings: list[str] = []
    fields: dict[str, Any] = {}
    checks: dict[str, bool] = {}

    if len(mrz) >= 2:
        l1, l2 = mrz[-2][:44].ljust(44, "<"), mrz[-1][:44].ljust(44, "<")
        fields["document_code"] = l1[0:2].replace("<", "")
        fields["issuing_state"] = l1[2:5].replace("<", "")
        names = l1[5:44].split("<<")
        fields["surname"] = names[0].replace("<", " ").strip()
        fields["given_names"] = names[1].replace("<", " ").strip() if len(names) > 1 else ""
        fields["passport_number"] = l2[0:9].replace("<", "")
        checks["passport_number_check"] = mrz_check_digit(l2[0:9]) == int(l2[9]) if l2[9].isdigit() \
            else False
        fields["nationality"] = l2[10:13].replace("<", "")
        fields["date_of_birth"] = l2[13:19]
        checks["dob_check"] = mrz_check_digit(l2[13:19]) == int(l2[19]) if l2[19].isdigit() else False
        fields["sex"] = l2[20]
        fields["expiry_date"] = l2[21:27]
        checks["expiry_check"] = mrz_check_digit(l2[21:27]) == int(l2[27]) if l2[27].isdigit() \
            else False
        composite = l2[0:10] + l2[13:20] + l2[21:28] + l2[28:43]
        checks["composite_check"] = mrz_check_digit(composite) == int(l2[43]) if l2[43].isdigit() \
            else False
        try:
            yy = int(fields["expiry_date"][0:2])
            expiry = date(2000 + yy if yy < 70 else 1900 + yy, int(fields["expiry_date"][2:4]),
                          int(fields["expiry_date"][4:6]))
            fields["expiry_iso"] = expiry.isoformat()
            checks["not_expired"] = expiry > date.today()
        except ValueError:
            checks["not_expired"] = False
            findings.append("Unparseable expiry date in MRZ")
    else:
        findings.append("No machine readable zone detected")
        match = re.search(r"passport\s*(?:no\.?|number)[:\s]*([A-Z0-9]{6,9})", text, re.I)
        if match:
            fields["passport_number"] = match.group(1)

    full_name = f"{fields.get('given_names', '')} {fields.get('surname', '')}".strip()
    similarity = name_similarity(full_name, case.applicant_name) if full_name else 0.0
    checks["name_matches_application"] = similarity >= 0.75
    if not checks["name_matches_application"] and full_name:
        findings.append(f"Name mismatch: document '{full_name}' vs application "
                        f"'{case.applicant_name}' (similarity {similarity})")

    passed = all(v for k, v in checks.items())
    doc.extracted_fields = {**(doc.extracted_fields or {}), **fields}
    doc.verification_status = "verified" if passed else "failed"
    doc.verification_notes = findings
    case.findings = [*(case.findings or []),
                     {"check": "passport", "status": doc.verification_status,
                      "detail": findings or "All MRZ checks passed"}]
    await ctx.session.flush()
    return {"document_id": doc.id, "verified": passed, "checks": checks, "fields": fields,
            "name_similarity": similarity, "findings": findings}


class VerifyPanArgs(BaseModel):
    case_number: str
    pan_number: str | None = Field(default=None, description="Override PAN, else read from OCR")


@tool(
    "verify_pan",
    "Validate an Indian PAN: structure, holder-type code, and name/OCR cross-check.",
    VerifyPanArgs,
    category="kyc",
    writes_data=True,
)
async def verify_pan(args: VerifyPanArgs, ctx: ToolContext) -> dict[str, Any]:
    case, doc = await _load_document(ctx, args.case_number, None, "pan")
    text = (doc.ocr_text or "").upper()
    pan = (args.pan_number or "").upper().strip()
    if not pan:
        match = re.search(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", text)
        pan = match.group(0) if match else ""
    checks = {
        "present": bool(pan),
        "format_valid": bool(PAN_RE.match(pan)) if pan else False,
    }
    holder_type = PAN_HOLDER_TYPES.get(pan[3]) if len(pan) == 10 else None
    checks["holder_type_known"] = holder_type is not None
    checks["holder_is_individual"] = holder_type == "Individual"
    surname_initial = pan[4] if len(pan) == 10 else ""
    applicant_surname = normalise_name(case.applicant_name).split()[-1:] or [""]
    checks["surname_initial_matches"] = bool(
        surname_initial and applicant_surname[0].upper().startswith(surname_initial)
    )
    findings = [k for k, v in checks.items() if not v]
    verified = checks["format_valid"] and checks["holder_type_known"]
    doc.extracted_fields = {**(doc.extracted_fields or {}), "pan_number": pan,
                            "holder_type": holder_type}
    doc.verification_status = "verified" if verified else "failed"
    case.findings = [*(case.findings or []),
                     {"check": "pan", "status": doc.verification_status,
                      "detail": f"PAN {pan or 'not found'}; failed checks: {findings}"}]
    await ctx.session.flush()
    return {"pan_number": pan, "verified": verified, "holder_type": holder_type, "checks": checks,
            "failed_checks": findings}


class VerifyAadhaarArgs(BaseModel):
    case_number: str
    aadhaar_number: str | None = None


@tool(
    "verify_aadhaar",
    "Validate an Aadhaar number using the Verhoeff checksum and OCR cross-check. "
    "Only the last four digits are ever returned.",
    VerifyAadhaarArgs,
    category="kyc",
    writes_data=True,
)
async def verify_aadhaar(args: VerifyAadhaarArgs, ctx: ToolContext) -> dict[str, Any]:
    case, doc = await _load_document(ctx, args.case_number, None, "aadhaar")
    text = doc.ocr_text or ""
    number = re.sub(r"\D", "", args.aadhaar_number or "")
    if not number:
        match = re.search(r"\b(\d{4}\s?\d{4}\s?\d{4})\b", text)
        number = re.sub(r"\D", "", match.group(1)) if match else ""
    checks = {
        "present": len(number) == 12,
        "checksum_valid": verhoeff_valid(number) if len(number) == 12 else False,
        "not_starting_with_0_or_1": bool(number) and number[0] not in "01",
    }
    verified = all(checks.values())
    doc.extracted_fields = {**(doc.extracted_fields or {}),
                            "aadhaar_last4": number[-4:] if number else None}
    doc.verification_status = "verified" if verified else "failed"
    case.findings = [*(case.findings or []),
                     {"check": "aadhaar", "status": doc.verification_status,
                      "detail": f"Verhoeff checksum {'passed' if verified else 'failed'}"}]
    await ctx.session.flush()
    return {"aadhaar_last4": number[-4:] if number else None, "verified": verified,
            "checks": checks}


class FaceMatchArgs(BaseModel):
    case_number: str
    threshold: float = Field(default=0.75, ge=0.0, le=1.0)


@tool(
    "face_match",
    "Compare the selfie against the photo page of the identity document and return a match score.",
    FaceMatchArgs,
    category="kyc",
    writes_data=True,
    timeout_seconds=120,
)
async def face_match(args: FaceMatchArgs, ctx: ToolContext) -> dict[str, Any]:
    case = (
        await ctx.session.execute(select(KycCase).where(KycCase.case_number == args.case_number))
    ).scalar_one_or_none()
    if case is None:
        raise NotFoundError(f"KYC case '{args.case_number}' not found")
    docs = (
        await ctx.session.execute(select(KycDocument).where(KycDocument.case_id == case.id))
    ).scalars().all()
    selfie = next((d for d in docs if d.doc_type == "selfie"), None)
    identity = next((d for d in docs if d.doc_type in {"passport", "aadhaar", "driving_licence"}),
                    None)
    if selfie is None or identity is None:
        raise ValidationError("Both a selfie and a photo identity document are required",
                              details={"has_selfie": selfie is not None,
                                       "has_identity_document": identity is not None})

    vision = [m for m in router.available_models(embeddings=False) if m.supports_vision]
    if not vision:
        raise ProviderNotConfiguredError(
            "Face matching requires a vision-capable model provider or a dedicated biometric "
            "service; none is configured",
            details={"configure": "ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY or a "
                                  "biometric provider"},
        )

    selfie_bytes = await store.get(selfie.artifact_uri.split("/", 3)[-1])
    id_bytes = await store.get(identity.artifact_uri.split("/", 3)[-1])
    prompt = (
        "You are a KYC biometric reviewer. Compare the two face images. Return ONLY JSON: "
        '{"same_person_likelihood": 0.0-1.0, "quality_issues": ["..."], "reasoning": "..."} '
        "Base the likelihood on facial geometry, not clothing or background."
    )
    from app.core.config import settings as _s
    from app.llm.base import http_client

    spec = vision[0]
    if spec.provider == "anthropic":
        content = [
            {"type": "text", "text": prompt},
            {"type": "image", "source": {"type": "base64", "media_type": selfie.mime_type,
                                         "data": base64.b64encode(selfie_bytes).decode()}},
            {"type": "image", "source": {"type": "base64", "media_type": identity.mime_type,
                                         "data": base64.b64encode(id_bytes).decode()}},
        ]
        resp = await http_client().post(
            f"{_s.anthropic_base_url}/v1/messages",
            headers={"x-api-key": _s.anthropic_api_key or "", "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": spec.id, "max_tokens": 700, "messages": [{"role": "user",
                                                                     "content": content}]},
        )
        raw = "".join(b.get("text", "") for b in resp.json().get("content", []))
    else:
        provider = router.provider(spec.provider)
        headers = {"Content-Type": "application/json"}
        if getattr(provider, "api_key", None):
            headers["Authorization"] = f"Bearer {provider.api_key}"  # type: ignore[union-attr]
        resp = await http_client().post(
            f"{provider.base_url}/chat/completions",  # type: ignore[union-attr]
            headers=headers,
            json={"model": spec.id, "max_tokens": 700, "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{selfie.mime_type};base64,"
                           f"{base64.b64encode(selfie_bytes).decode()}"}},
                {"type": "image_url", "image_url": {
                    "url": f"data:{identity.mime_type};base64,"
                           f"{base64.b64encode(id_bytes).decode()}"}},
            ]}]},
        )
        raw = resp.json()["choices"][0]["message"]["content"]

    parsed: dict[str, Any] = {}
    match = re.search(r"\{.*\}", raw or "", re.S)
    if match:
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            parsed = {}
    score = float(parsed.get("same_person_likelihood", 0.0))
    case.face_match_score = score
    case.findings = [*(case.findings or []),
                     {"check": "face_match", "status": "pass" if score >= args.threshold else "fail",
                      "detail": f"score {score:.2f} (threshold {args.threshold})"}]
    await ctx.session.flush()
    return {
        "score": round(score, 4),
        "threshold": args.threshold,
        "match": score >= args.threshold,
        "quality_issues": parsed.get("quality_issues", []),
        "reasoning": parsed.get("reasoning", "")[:600],
        "engine": f"vision:{spec.id}",
    }


class AddressArgs(BaseModel):
    case_number: str
    proof_document_type: str = Field(default="utility_bill")


@tool(
    "validate_address",
    "Cross-check the declared address against the address proof document text.",
    AddressArgs,
    category="kyc",
    writes_data=True,
)
async def validate_address(args: AddressArgs, ctx: ToolContext) -> dict[str, Any]:
    case, doc = await _load_document(ctx, args.case_number, None, args.proof_document_type)
    declared = case.declared_address or {}
    declared_text = " ".join(str(v) for v in declared.values() if v)
    if not declared_text:
        raise ValidationError("Case has no declared address to validate")
    proof_text = (doc.ocr_text or "")
    if not proof_text:
        raise ValidationError("Run ocr_document on the address proof first")

    declared_tokens = set(normalise_name(declared_text).split())
    proof_tokens = set(normalise_name(proof_text).split())
    overlap = declared_tokens & proof_tokens
    coverage = len(overlap) / max(len(declared_tokens), 1)
    postcode = str(declared.get("postcode") or "")
    postcode_found = bool(postcode) and re.sub(r"\s", "", postcode) in re.sub(r"\s", "", proof_text)
    bill_date = None
    date_match = re.search(r"(\d{2}[/-]\d{2}[/-]\d{4})", proof_text)
    if date_match:
        bill_date = date_match.group(1)

    verified = coverage >= 0.5 and (postcode_found or coverage >= 0.7)
    case.address_verified = verified
    case.findings = [*(case.findings or []),
                     {"check": "address", "status": "pass" if verified else "fail",
                      "detail": f"token coverage {coverage:.2f}, postcode "
                                f"{'found' if postcode_found else 'not found'}"}]
    await ctx.session.flush()
    return {"verified": verified, "token_coverage": round(coverage, 3),
            "postcode_found": postcode_found, "matched_tokens": sorted(overlap)[:25],
            "document_date": bill_date, "proof_type": args.proof_document_type}


# --- screening -----------------------------------------------------------------
class ScreeningArgs(BaseModel):
    full_name: str
    date_of_birth: str | None = None
    nationality: str | None = None
    threshold: float = Field(default=0.82, ge=0.5, le=1.0)
    lists: list[str] | None = Field(default=None, description="Restrict to specific list names")


async def _screen(ctx: ToolContext, args: ScreeningArgs, pep_only: bool) -> dict[str, Any]:
    stmt = select(SanctionsEntry)
    if pep_only:
        stmt = stmt.where(SanctionsEntry.is_pep.is_(True))
    else:
        stmt = stmt.where(SanctionsEntry.is_pep.is_(False))
    if args.lists:
        stmt = stmt.where(SanctionsEntry.list_name.in_(args.lists))
    entries = (await ctx.session.execute(stmt)).scalars().all()
    target = normalise_name(args.full_name)
    hits: list[dict[str, Any]] = []
    for entry in entries:
        candidates = [entry.normalised_name, *(normalise_name(a) for a in (entry.aliases or []))]
        best = max((name_similarity(target, c) for c in candidates if c), default=0.0)
        if best >= args.threshold:
            dob_match = None
            if args.date_of_birth and entry.date_of_birth:
                dob_match = args.date_of_birth[:4] == entry.date_of_birth[:4]
            hits.append({
                "list": entry.list_name,
                "matched_name": entry.full_name,
                "aliases": entry.aliases,
                "score": best,
                "entry_type": entry.entry_type,
                "program": entry.program,
                "position": entry.position,
                "nationality": entry.nationality,
                "date_of_birth": entry.date_of_birth,
                "dob_year_match": dob_match,
                "source_url": entry.source_url,
                "listed_on": entry.listed_on.isoformat() if entry.listed_on else None,
            })
    hits.sort(key=lambda h: h["score"], reverse=True)
    return {
        "query": args.full_name,
        "records_screened": len(entries),
        "threshold": args.threshold,
        "hit_count": len(hits),
        "hits": hits[:20],
        "clear": not hits,
    }


@tool(
    "screen_sanctions",
    "Fuzzy-screen a name against loaded sanctions watchlists (OFAC/UN/EU/RBI/internal).",
    ScreeningArgs,
    category="kyc",
    timeout_seconds=45,
)
async def screen_sanctions(args: ScreeningArgs, ctx: ToolContext) -> dict[str, Any]:
    return await _screen(ctx, args, pep_only=False)


@tool(
    "screen_pep",
    "Fuzzy-screen a name against the politically exposed persons list.",
    ScreeningArgs,
    category="kyc",
    timeout_seconds=45,
)
async def screen_pep(args: ScreeningArgs, ctx: ToolContext) -> dict[str, Any]:
    return await _screen(ctx, args, pep_only=True)


class RiskArgs(BaseModel):
    case_number: str
    occupation: str | None = None
    annual_income: float | None = None
    source_of_funds: str | None = None
    expected_monthly_volume: float | None = None


HIGH_RISK_COUNTRIES = {"IR", "KP", "SY", "AF", "MM", "YE", "SS", "CU", "VE"}
HIGH_RISK_OCCUPATIONS = {"casino", "crypto", "arms", "precious metals", "money service",
                         "shell company", "politician", "diplomat"}


@tool(
    "calculate_kyc_risk_score",
    "Compute a weighted customer risk score and band from screening, geography and profile.",
    RiskArgs,
    category="kyc",
    writes_data=True,
)
async def calculate_kyc_risk_score(args: RiskArgs, ctx: ToolContext) -> dict[str, Any]:
    case = (
        await ctx.session.execute(select(KycCase).where(KycCase.case_number == args.case_number))
    ).scalar_one_or_none()
    if case is None:
        raise NotFoundError(f"KYC case '{args.case_number}' not found")

    components: list[dict[str, Any]] = []

    sanctions_weight = 60.0 if case.sanctions_hits else 0.0
    components.append({"factor": "sanctions_hits", "weight": sanctions_weight,
                       "detail": f"{len(case.sanctions_hits or [])} hits"})
    pep_weight = 25.0 if case.pep_hits else 0.0
    components.append({"factor": "pep_exposure", "weight": pep_weight,
                       "detail": f"{len(case.pep_hits or [])} hits"})

    country = (case.nationality or "").upper()[:2]
    geo_weight = 20.0 if country in HIGH_RISK_COUNTRIES else 0.0
    components.append({"factor": "geography", "weight": geo_weight, "detail": country})

    occupation = (args.occupation or "").lower()
    occ_weight = 15.0 if any(term in occupation for term in HIGH_RISK_OCCUPATIONS) else 0.0
    components.append({"factor": "occupation", "weight": occ_weight, "detail": occupation or "n/a"})

    doc_weight = 0.0
    docs = (
        await ctx.session.execute(select(KycDocument).where(KycDocument.case_id == case.id))
    ).scalars().all()
    failed_docs = [d for d in docs if d.verification_status == "failed"]
    if failed_docs:
        doc_weight = 12.0 * len(failed_docs)
    components.append({"factor": "document_verification", "weight": doc_weight,
                       "detail": f"{len(failed_docs)}/{len(docs)} documents failed"})

    face_weight = 0.0
    if case.face_match_score is not None and case.face_match_score < 0.75:
        face_weight = 18.0
    components.append({"factor": "biometric_match", "weight": face_weight,
                       "detail": f"score {case.face_match_score}"})

    address_weight = 0.0 if case.address_verified else 8.0
    components.append({"factor": "address_verification", "weight": address_weight,
                       "detail": "verified" if case.address_verified else "unverified"})

    volume_weight = 0.0
    if args.expected_monthly_volume and args.annual_income:
        ratio = (args.expected_monthly_volume * 12) / max(args.annual_income, 1)
        if ratio > 1.5:
            volume_weight = min(20.0, 10.0 * ratio)
        components.append({"factor": "volume_vs_income", "weight": volume_weight,
                           "detail": f"ratio {ratio:.2f}"})

    score = min(100.0, sum(c["weight"] for c in components))
    band = "critical" if score >= 75 else "high" if score >= 50 else "medium" if score >= 25 \
        else "low"
    case.risk_score = score
    case.risk_band = band
    await ctx.session.flush()
    return {"case_number": case.case_number, "risk_score": round(score, 2), "risk_band": band,
            "components": components,
            "recommended_action": {
                "low": "Standard due diligence - approve",
                "medium": "Standard due diligence with periodic review",
                "high": "Enhanced due diligence required before approval",
                "critical": "Escalate to compliance; do not onboard without MLRO sign-off",
            }[band]}


class OnboardingReportArgs(BaseModel):
    case_number: str
    decision: str = Field(description="approve|reject|refer")
    rationale: str


@tool(
    "generate_onboarding_report",
    "Produce and store the final KYC onboarding report artifact for the case. "
    "Requires human approval.",
    OnboardingReportArgs,
    category="kyc",
    writes_data=True,
    idempotent=False,
    requires_approval=True,
    approval_risk="high",
    timeout_seconds=60,
)
async def generate_onboarding_report(args: OnboardingReportArgs, ctx: ToolContext) -> dict[str, Any]:
    case = (
        await ctx.session.execute(select(KycCase).where(KycCase.case_number == args.case_number))
    ).scalar_one_or_none()
    if case is None:
        raise NotFoundError(f"KYC case '{args.case_number}' not found")
    docs = (
        await ctx.session.execute(select(KycDocument).where(KycDocument.case_id == case.id))
    ).scalars().all()

    report = {
        "report_id": f"KYC-RPT-{uuid.uuid4().hex[:8].upper()}",
        "generated_at": datetime.now(UTC).isoformat(),
        "case_number": case.case_number,
        "applicant": {
            "name": case.applicant_name,
            "date_of_birth": case.date_of_birth.isoformat() if case.date_of_birth else None,
            "nationality": case.nationality,
            "declared_address": case.declared_address,
        },
        "documents": [
            {"type": d.doc_type, "status": d.verification_status,
             "classification_confidence": d.classification_confidence,
             "extracted_fields": d.extracted_fields, "notes": d.verification_notes}
            for d in docs
        ],
        "screening": {"sanctions_hits": case.sanctions_hits, "pep_hits": case.pep_hits},
        "biometrics": {"face_match_score": case.face_match_score},
        "address_verified": case.address_verified,
        "risk": {"score": case.risk_score, "band": case.risk_band},
        "findings": case.findings,
        "decision": args.decision,
        "rationale": args.rationale,
        "decided_by": ctx.user_email or "ai_agent",
        "execution_id": ctx.execution_id,
    }
    key = f"kyc/{case.case_number}/{report['report_id']}.json"
    stored = await store.put(key, json.dumps(report, indent=2).encode(), "application/json")

    case.decision = args.decision
    case.status = {"approve": "approved", "reject": "rejected", "refer": "referred"}.get(
        args.decision, "in_progress"
    )
    case.report_uri = stored["uri"]
    case.decided_at = datetime.now(UTC)
    case.decided_by = ctx.user_email or "ai_agent"
    case.execution_id = ctx.execution_id
    await ctx.session.flush()
    return {"report_id": report["report_id"], "artifact": stored, "decision": args.decision,
            "case_status": case.status}
