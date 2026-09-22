import os
from collections.abc import Sequence
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_secret
from app.models.ai import ChunkHit, ChunkSearchRequest, ExtractMetadataRequest
from app.models.core_models import ResourceEmbedding
from app.utils.auth_utils import authorize_user, permission_filter
from app.utils.permissions import Permission, Principal

router = APIRouter(tags=["ai"])
engine = get_engine()

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


def search_chunks(
    session: Session,
    principal: Principal,
    query_vector: Sequence[float],
    k: int = 20,
    probes: int = 10,
    model_name: Optional[str] = None,
    resource_mfids: Optional[Sequence[str]] = None,
) -> list[ChunkHit]:
    """Return the nearest readable chunks by cosine distance."""
    session.execute(
        text("SELECT set_config('ivfflat.probes', :probes, true)"),
        {"probes": str(probes)},
    )

    embedding_column = ResourceEmbedding.__table__.c.embedding
    distance = embedding_column.cosine_distance(query_vector).label("distance")
    statement = (
        select(ResourceEmbedding, distance)
        .where(
            permission_filter(
                principal,
                ResourceEmbedding,
                Permission.READ,
                resource_id_column=ResourceEmbedding.resource_mfid,
            )
        )
        .order_by(
            distance,
            ResourceEmbedding.resource_mfid,
            ResourceEmbedding.chunk_index,
            ResourceEmbedding.id,
        )
        .limit(k)
    )

    if model_name is not None:
        statement = statement.where(ResourceEmbedding.model_name == model_name)
    if resource_mfids is not None:
        statement = statement.where(ResourceEmbedding.resource_mfid.in_(resource_mfids))

    return [
        ChunkHit(
            resource_mfid=row.resource_mfid,
            chunk_index=row.chunk_index,
            chunk_text=row.chunk_text,
            distance=float(distance_value),
        )
        for row, distance_value in session.execute(statement).all()
    ]


@router.post("/resources/embeddings/search", response_model=list[ChunkHit])
def search_resource_chunks(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    body: ChunkSearchRequest,
):
    """Search 768-dimensional resource text chunks visible to the caller."""
    with Session(engine) as session:
        return search_chunks(
            session=session,
            principal=auth_res,
            query_vector=body.query_vector,
            k=body.k,
            probes=body.probes,
            model_name=body.model_name,
            resource_mfids=body.resource_mfids,
        )


@router.post("/extract_metadata")
def extract_metadata(
    auth_res: Annotated[Principal, Depends(authorize_user)],
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
