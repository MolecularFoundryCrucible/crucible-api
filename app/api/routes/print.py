import json
import re
import threading
import time
import uuid
from typing import Annotated, Literal, Optional

import paho.mqtt.client as mqtt
import requests
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.general_utils import get_secret, is_valid_mfid
from app.utils.auth_utils import authorize_user
from app.utils.constants import label_printer_status_url
from app.utils.permissions import Principal

router = APIRouter(tags=["print"])

MQTT_BROKER = "mqtt.mfdata.org"
MQTT_PORT = 8883
MQTT_USERNAME = "crucible-printers"

# Derived from crucible-label-printer's own retry behavior: the Pi retries a
# failed ptouch-print up to twice with a 3s gap (it can still be feeding/
# cutting the previous label), so up to 6s may pass before it reports
# anything. 8s leaves margin so a success on the last retry isn't reported
# as a timeout here.
RESULT_WAIT_TIMEOUT = 8

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
    status: Literal["ok", "error", "timeout"]
    detail: Optional[str] = None


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


def _publish_and_await_result(printer_id: str, mfid: str, name: str) -> tuple[dict, Optional[dict]]:
    """Publish a print job and wait briefly for a matching result.

    Returns (payload, result): result is the {job_id, status} message from
    crucible-printer/<printer_id>/result if one arrived within
    RESULT_WAIT_TIMEOUT, else None.
    """
    mqtt_password = get_secret("MQTT_PASSWORD", "mqtt-crucible-printers-passwd/versions/1")

    job_id = str(uuid.uuid4())
    payload = {"job_id": job_id, "mfid": mfid, "name": name, "ts": time.time()}
    print_topic = f"crucible-printer/{printer_id}/print"
    result_topic = f"crucible-printer/{printer_id}/result"

    result: dict = {}
    received = threading.Event()

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if data.get("job_id") == job_id:
            result.update(data)
            received.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"crucible-api-{job_id}")
    client.username_pw_set(MQTT_USERNAME, mqtt_password)
    client.tls_set()
    client.on_message = on_message
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    # Subscribe before publishing so a fast result can't arrive before we're
    # listening for it.
    client.subscribe(result_topic, qos=1)
    client.loop_start()
    try:
        info = client.publish(print_topic, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=10)
        received.wait(timeout=RESULT_WAIT_TIMEOUT)
    finally:
        client.loop_stop()
        client.disconnect()

    return payload, (dict(result) if received.is_set() else None)


def _printer_online(printer_id: str) -> Optional[bool]:
    """Best-effort check against crucible-label-printer-status.

    None if the service isn't configured or the check itself fails -- this
    is a soft dependency that must never block or fail the request.
    """
    if not label_printer_status_url:
        return None
    try:
        resp = requests.get(f"{label_printer_status_url}/api/printers", timeout=3)
        resp.raise_for_status()
        printers = {p["printer_id"]: p for p in resp.json()}
    except (requests.RequestException, ValueError, KeyError):
        return None
    printer = printers.get(printer_id)
    if printer is None:
        return False
    return printer.get("state") not in (None, "offline")


def _offline_detail(printer_id: str) -> Optional[str]:
    if _printer_online(printer_id) is False:
        return "Requested printer not currently online. Check spelling and the crucible-printer dashboard."
    return None


@router.post("/print/barcode", response_model=BarcodePrintResponse, status_code=200)
def print_barcode(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    body: BarcodePrintRequest,
    response: Response,
):
    '''Publishes a barcode print job and waits briefly for a print result.

    Always returns 200; `status` distinguishes confirmed success ("ok") from
    an explicit printer-reported failure ("error") or no result within the
    wait window ("timeout"). On "error"/"timeout", `detail` may note whether
    the requested printer currently appears online, via
    crucible-label-printer-status, to help diagnose typos vs. offline
    hardware.
    '''
    printer_id = _validate_printer_id(body.printer_id)
    mfid = _validate_mfid(body.mfid)
    payload, result = _publish_and_await_result(printer_id, mfid, body.name)

    if result is None:
        status, detail = "timeout", _offline_detail(printer_id)
    elif result.get("status") == "ok":
        status, detail = "ok", None
    else:
        status, detail = "error", _offline_detail(printer_id)

    response.headers["Cache-Control"] = "no-store"
    return BarcodePrintResponse(printer_id=printer_id, status=status, detail=detail, **payload)
