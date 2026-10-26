import json
import re
import threading
import time
import uuid
from typing import Annotated, Literal, Optional

import paho.mqtt.client as mqtt
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.config import get_secret
from app.general_utils import is_valid_mfid
from app.utils.auth_utils import authorize_user
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

# A printer publishes a retained status heartbeat every 30s
# (crucible-label-printer's heartbeat()); crucible-label-printer-status uses
# the same 90s (3x) threshold to call a heartbeat stale.
STATUS_STALE_THRESHOLD = 90
# Retained status messages normally arrive immediately on subscribe; this just
# bounds a dead/unreachable broker connection.
STATUS_WAIT_TIMEOUT = 3

# Ceiling for the broker's SUBACK when listing printers; normally one
# network round trip, so this only bounds a slow/unreachable broker.
PRINTER_LIST_WAIT = 0.5
# Small fixed buffer after SUBACK for the broker to finish replaying
# retained messages for every printer_id matched by the wildcard.
PRINTER_LIST_REPLAY_BUFFER = 0.15

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


class PrinterStatus(BaseModel):
    printer_id: str
    online: bool
    ts: Optional[float] = None


class PrinterListResponse(BaseModel):
    printers: list[PrinterStatus]


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


def _publish_and_await_result(printer_id: str, mfid: str, name: str) -> tuple[dict, Optional[dict], Optional[dict]]:
    """Publish a print job and wait briefly for a matching result.

    Also subscribes to the printer's retained status heartbeat on the same
    connection, so a failure/timeout can be explained without a second
    round trip.

    Returns (payload, result, status): result is the {job_id, status}
    message from crucible-printer/<printer_id>/result if one arrived within
    RESULT_WAIT_TIMEOUT, else None. status is the printer's retained
    {state, hostname, ip, ts} status message if one arrived, else None --
    None means no such printer_id has ever published a status (or the
    broker couldn't be reached in time), not that it's merely offline.
    """
    mqtt_password = get_secret("MQTT_PASSWORD")

    job_id = str(uuid.uuid4())
    payload = {"job_id": job_id, "mfid": mfid, "name": name, "ts": time.time()}
    print_topic = f"crucible-printer/{printer_id}/print"
    result_topic = f"crucible-printer/{printer_id}/result"
    status_topic = f"crucible-printer/{printer_id}/status"

    result: dict = {}
    received = threading.Event()
    status: dict = {}
    status_received = threading.Event()

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if msg.topic == result_topic:
            if data.get("job_id") == job_id:
                result.update(data)
                received.set()
        elif msg.topic == status_topic:
            status.clear()
            status.update(data)
            status_received.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"crucible-api-{job_id}")
    client.username_pw_set(MQTT_USERNAME, mqtt_password)
    client.tls_set()
    client.on_message = on_message
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    # Subscribe before publishing so a fast result can't arrive before we're
    # listening for it. Status is retained, so it's delivered immediately
    # on subscribe, well before the result (if any) arrives.
    client.subscribe(result_topic, qos=1)
    client.subscribe(status_topic, qos=1)
    client.loop_start()
    try:
        info = client.publish(print_topic, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=10)
        received.wait(timeout=RESULT_WAIT_TIMEOUT)
        if not status_received.is_set():
            status_received.wait(timeout=STATUS_WAIT_TIMEOUT)
    finally:
        client.loop_stop()
        client.disconnect()

    return (
        payload,
        dict(result) if received.is_set() else None,
        dict(status) if status_received.is_set() else None,
    )


def _printer_online(status: Optional[dict]) -> bool:
    """Interpret a printer's retained status message.

    A missing status (never published / broker unreachable in time) and an
    explicit "offline" last-will both mean "not online". A stale "online"
    heartbeat (crucible-label-printer heartbeats every 30s;
    crucible-label-printer-status uses the same 90s/3x threshold to call one
    stale) is also not online -- the process may still be running but hasn't
    reported in a while.
    """
    if status is None or status.get("state") != "online":
        return False
    ts = status.get("ts")
    if ts is None:
        # The very first "online" status after connect is published without
        # a ts (only the periodic heartbeat includes one); no evidence of
        # staleness, so trust it.
        return True
    return (time.time() - ts) <= STATUS_STALE_THRESHOLD


def _offline_detail(status: Optional[dict]) -> Optional[str]:
    if not _printer_online(status):
        return "Requested printer not currently online. Check spelling and the crucible-printer dashboard."
    return None


def _list_printer_statuses() -> dict[str, dict]:
    """Collect the retained status message for every printer_id that has
    ever published one, via a crucible-printer/+/status wildcard subscribe.

    Waits for the broker's SUBACK (confirming the subscription is live)
    rather than a flat sleep, then a short fixed buffer for the broker to
    finish replaying retained messages -- faster than a worst-case timeout
    in the common case, while still correct if the broker is slow.

    Returns {printer_id: status_dict}. A printer that has never published a
    status (e.g. configured in inventory but never powered on) is not
    discoverable this way and will not appear -- there is no separate
    registry of printer_ids to fall back on.
    """
    mqtt_password = get_secret("MQTT_PASSWORD")

    statuses: dict[str, dict] = {}
    subscribed = threading.Event()

    def on_message(client, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) != 3:
            return
        printer_id = parts[1]
        try:
            data = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        statuses[printer_id] = data

    def on_subscribe(client, userdata, mid, reason_code_list, properties):
        subscribed.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"crucible-api-list-{uuid.uuid4()}")
    client.username_pw_set(MQTT_USERNAME, mqtt_password)
    client.tls_set()
    client.on_message = on_message
    client.on_subscribe = on_subscribe
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.subscribe("crucible-printer/+/status", qos=1)
    client.loop_start()
    try:
        subscribed.wait(timeout=PRINTER_LIST_WAIT)
        time.sleep(PRINTER_LIST_REPLAY_BUFFER)
    finally:
        client.loop_stop()
        client.disconnect()

    return statuses


@router.get("/print/printers", response_model=PrinterListResponse)
def list_printers(
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''Lists printers discovered from their retained MQTT status heartbeat.

    Only printers that have published at least one status message appear;
    there is no separate registry of valid printer_id values. `online`
    uses the same staleness rule as `POST /print/barcode`'s `detail` field.
    '''
    statuses = _list_printer_statuses()
    printers = [
        PrinterStatus(
            printer_id=printer_id,
            online=_printer_online(status),
            ts=status.get("ts"),
        )
        for printer_id, status in sorted(statuses.items())
    ]
    return PrinterListResponse(printers=printers)


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
    the requested printer's retained MQTT status currently looks online, to
    help diagnose typos vs. offline hardware.
    '''
    printer_id = _validate_printer_id(body.printer_id)
    mfid = _validate_mfid(body.mfid)
    payload, result, printer_status = _publish_and_await_result(printer_id, mfid, body.name)

    if result is None:
        status, detail = "timeout", _offline_detail(printer_status)
    elif result.get("status") == "ok":
        status, detail = "ok", None
    else:
        status, detail = "error", _offline_detail(printer_status)

    response.headers["Cache-Control"] = "no-store"
    return BarcodePrintResponse(printer_id=printer_id, status=status, detail=detail, **payload)
