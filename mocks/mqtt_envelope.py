#!/usr/bin/env python3
"""Nachrichtenformat und Brokerverbindung fuer die beiden MQTT-Mocks.

Der Envelope folgt den Beispieldateien in data/reference: ein flacher Kopf
mit publisher-Angaben und ein data-Objekt. Der Plan steckt darin als JSON-String,
nicht als eingebettetes Objekt - das ist Absicht und Teil des Kontrakts.
"""

import json
from datetime import datetime

import paho.mqtt.client as mqtt

TOPIC_PLAN = "plan"
TOPIC_ACTION = "action"
QOS = 1

PUBLISHER_REASONER = {
    "publisher-id": 123,
    "publisher-name": "DFKI",
    "publisher-version": 1,
    "publisher-description": "plan description",
}

PUBLISHER_CLIENT = {
    "publisher-id": 123,
    "publisher-name": "ifak",
    "publisher-version": 1,
    "publisher-description": "action detection",
}


def now_iso():
    """Zeitstempel mit lokalem Offset, z.B. 2026-08-17T15:09:00.000+02:00."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def wrap(publisher, data):
    return {"message-type": "data", **publisher, "timestamp": now_iso(), "data": data}


def plan_message(plan):
    """Plan-Nachricht; der Plan wird als JSON-String eingebettet."""
    return wrap(PUBLISHER_REASONER,
                {"plan": json.dumps(plan, ensure_ascii=False, separators=(",", ":"))})


def terminal_message(kind, assembly_id, observed, reason=None):
    """planComplete oder noMatchingPlan."""
    data = {"type": kind, "assemblyID": assembly_id, "observed": list(observed)}
    if reason is not None:
        data["reason"] = reason
    return wrap(PUBLISHER_REASONER, data)


def action_message(assembly_id, action_id):
    return wrap(PUBLISHER_CLIENT, {"type": "detectedAction",
                                   "assemblyID": assembly_id,
                                   "actionID": action_id})


def read_data(payload):
    """Liefert das data-Objekt einer Nachricht.

    Wirft ValueError bei kaputtem JSON oder fehlendem data-Feld - der Aufrufer
    soll eine Stoerung sehen und nicht still auf einem leeren dict weiterlaufen.
    """
    try:
        msg = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Nachricht ist kein gueltiges JSON: {exc}") from exc
    if not isinstance(msg, dict) or "data" not in msg:
        raise ValueError("Nachricht hat kein data-Feld")
    data = msg["data"]
    if not isinstance(data, dict):
        raise ValueError(f"data-Feld ist kein Objekt, sondern {type(data).__name__}")
    return data


def connect(host, port, client_id):
    """Verbindet zum Broker und startet die Netzwerkschleife im Hintergrund."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.connect(host, port, keepalive=60)
    client.loop_start()
    return client
