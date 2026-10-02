import json
import re
import time
import uuid
from typing import Annotated

import paho.mqtt.client as mqtt
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.general_utils import get_secret, is_valid_mfid
from app.utils.auth_utils import authorize_user
from app.utils.permissions import Principal

router = APIRouter(tags=["print"])

MQTT_BROKER = "mqtt.mfdata.org"
MQTT_PORT = 8883
MQTT_USERNAME = "crucible-printers"

_PRINTER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class BarcodePrintRequest(BaseModel):
    printer_id: str = Field(description="Target printer identifier, e.g. 'spinbot'.")
    mfid: str = Field(description="MFID of the resource to print.")
    name: str = Field(description="Human-readable label text.")


class BarcodePrintResponse(BaseModel):
    job_id: str
    printer_id: str
    mfid: str
    name: str
    ts: float


def _validate_printer_id(printer_id: str) -> str:
    # printer_id is interpolated directly into the MQTT topic below, so it must
    # be restricted to a safe character set to prevent topic injection (e.g.
    # '/', '+', '#' would let a caller target a different topic or subscribe
    # with a wildcard).
    if not _PRINTER_ID_RE.fullmatch(printer_id):
        raise HTTPException(
            status_code=422,
            detail="printer_id must be 1-64 chars of letters, digits, '_' or '-'",
        )
    return printer_id


def _validate_mfid(mfid: str) -> str:
    # A tray's MFID is printed before its dataset is uploaded and ingested, so
    # there is no row to look up or an owning resource to check access
    # against at print time -- this only confirms the caller sent a
    # well-formed MFID, not that it currently exists in the database.
    if not is_valid_mfid(mfid):
        raise HTTPException(status_code=422, detail="mfid is not a valid MFID")
    return mfid


def _publish_barcode_print_job(printer_id: str, mfid: str, name: str) -> dict:
    """Publish a print job to crucible-printer/<printer_id>/print. Returns the payload."""
    mqtt_password = get_secret("MQTT_PASSWORD", "mqtt-crucible-printers-passwd/versions/1")

    payload = {
        "job_id": str(uuid.uuid4()),
        "mfid": mfid,
        "name": name,
        "ts": time.time(),
    }
    topic = f"crucible-printer/{printer_id}/print"

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"crucible-api-{payload['job_id']}")
    client.username_pw_set(MQTT_USERNAME, mqtt_password)
    client.tls_set()
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()
    try:
        info = client.publish(topic, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=10)
    finally:
        client.loop_stop()
        client.disconnect()

    return payload


@router.post("/print/barcode", response_model=BarcodePrintResponse, status_code=202)
def print_barcode(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    body: BarcodePrintRequest,
    response: Response,
):
    '''Publishes a barcode print job to the given printer over MQTT.

    Fire-and-forget: a 202 response means the job was published to the
    broker, not that it was received or printed by the target device.
    '''
    printer_id = _validate_printer_id(body.printer_id)
    mfid = _validate_mfid(body.mfid)
    payload = _publish_barcode_print_job(printer_id, mfid, body.name)
    response.headers["Cache-Control"] = "no-store"
    return BarcodePrintResponse(printer_id=printer_id, **payload)
