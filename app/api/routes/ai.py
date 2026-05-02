import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.general_utils import get_secret
from app.models.ai import ExtractMetadataRequest
from app.utils.auth_utils import authorize_user

router = APIRouter(tags=["ai"])

_anthropic_client = None


def _get_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        api_key = get_secret("ANTHROPIC_AUTH_TOKEN", "mfdata-cborg-api-key/versions/2")
        base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.cborg.lbl.gov")
        _anthropic_client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    return _anthropic_client


SYSTEM_PROMPT = (
    "You are a scientific metadata extractor specializing in laboratory notebooks. "
    "Analyze the provided image(s) and extract all relevant scientific metadata as a single JSON object. "
    "Include fields such as sample identifiers, dates, measurements, conditions, instrument settings, "
    "observations, and any other structured information visible in the notebook. "
    "Return only valid JSON with no additional text or markdown."
)


@router.post("/extract_metadata")
def extract_metadata(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    body: ExtractMetadataRequest,
):
    """
    Extract scientific metadata from one or more lab notebook images using Claude vision.
    Returns a free-form JSON object with the extracted metadata.
    Pass an optional context string to guide the extraction (e.g. instrument type, experiment class).
    """
    if not body.images:
        raise HTTPException(status_code=400, detail="At least one image is required.")

    try:
        if body.api_key:
            import anthropic
            base_url = body.api_url or os.getenv("ANTHROPIC_BASE_URL", "https://api.cborg.lbl.gov")
            client = anthropic.Anthropic(api_key=body.api_key, base_url=base_url)
        else:
            client = _get_client()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not initialise AI client: {e}")

    content = []
    for img in body.images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": img.media_type, "data": img.data},
        })

    user_text = "Extract metadata from these lab notebook page(s) as a JSON object."
    if body.context:
        user_text += f" Additional context: {body.context}"
    content.append({"type": "text", "text": user_text})

    try:
        model = os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-4-5")
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"AI extraction failed: {e}")

    import json
    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=422,
            detail=f"Model did not return valid JSON. Raw response: {raw[:500]}",
        )
