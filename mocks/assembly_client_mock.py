#!/usr/bin/env python3
"""Assembly-Client-Mock.

Spielt eine vorgegebene Schrittfolge aus einer Szenariodatei ab und meldet jeden
Schritt ueber MQTT. Der Client leitet seine Schritte bewusst NICHT aus dem
empfangenen Plan ab - nur so lassen sich Szenarien gezielt fahren, auch solche,
die kein gueltiger Plan sind.

Lockstep: nach jeder empfangenen Plan-Nachricht wird genau ein Step gesendet.

Startreihenfolge: dieser Client muss laufen, BEVOR der Reasoner gestartet wird.
Der Reasoner sendet seine erste Plan-Nachricht unaufgefordert und ohne retain -
ohne wartenden Client wuerde diese Nachricht verloren gehen.
"""

import argparse
import json
import queue
import sys
import time
from pathlib import Path

from . import mqtt_envelope as env

TERMINAL_TYPES = ("planComplete", "noMatchingPlan")


def load_scenario(path):
    """Liest eine Szenariodatei und prueft die Pflichtfelder."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Szenariodatei enthaelt kein Objekt")
    if not data.get("assemblyID"):
        raise ValueError("Szenariodatei ohne assemblyID")
    steps = data.get("steps")
    if not steps:
        raise ValueError("Szenariodatei ohne steps")
    for i, step in enumerate(steps):
        if not step.get("actionID"):
            raise ValueError(f"Step {i} ohne actionID")
    return data


class Replayer:
    """Gibt die Steps der Szenariodatei einen nach dem anderen heraus."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.assembly_id = scenario["assemblyID"]
        self._queue = [s["actionID"] for s in scenario["steps"]]
        self._at = 0
        self.sent = []

    @property
    def exhausted(self):
        return self._at >= len(self._queue)

    def next_action(self):
        if self.exhausted:
            return None
        action_id = self._queue[self._at]
        self._at += 1
        self.sent.append(action_id)
        return action_id


def read_plan_id(raw_plan):
    """Die id des Plans aus dem plan-Feld einer Plan-Nachricht.

    `raw_plan` ist der JSON-String, als der der Plan im Envelope steckt (siehe
    mqtt_envelope.plan_message). Wirft ValueError, wenn sich keine id lesen
    laesst - wie `read_data` und `load_scenario`; der Aufrufer soll eine
    Stoerung sehen und nicht still auf None weiterlaufen.

    Der Client faengt das ab, statt sich zu beenden: die Nachricht WAR eine
    Plan-Nachricht, er wertet vom Plan ohnehin nur die id aus, und im Lockstep
    liesse ein Abbruch hier beide Seiten warten.
    """
    try:
        plan = json.loads(raw_plan)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Plan-Nachricht ohne lesbares JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise ValueError(f"plan-Feld ist kein Objekt, sondern "
                         f"{type(plan).__name__}")
    plan_id = plan.get("id")
    if plan_id is None:
        raise ValueError("Plan ohne id-Feld")
    return plan_id


def run_mqtt(replayer, args):
    """Verbindet, spielt das Szenario im Lockstep ab und wartet auf das Terminalsignal.

    Das MQTT-Netzwerk laeuft in einem eigenen Thread (siehe mqtt_envelope.connect).
    on_message laeuft auf diesem Thread und darf ihn nicht blockieren - deshalb
    schlaeft und publiziert es nicht selbst, sondern legt nur ein Token in eine
    Queue. Das Schlafen (--step-delay) und das Publizieren passieren im
    Hauptthread, damit die Netzwerkschleife (inkl. Keepalive) nie stillsteht.
    """
    client = env.connect(args.broker_host, args.broker_port, "assembly-client-mock")
    state = {"terminal": None}
    last_plan_id = None
    plan_queue = queue.Queue()

    def on_message(_client, _userdata, msg):
        try:
            data = env.read_data(msg.payload)
        except ValueError as exc:
            print(f"[client] verworfen: {exc}", file=sys.stderr)
            return

        kind = data.get("type")
        if kind in TERMINAL_TYPES:
            state["terminal"] = data
            return
        if "plan" not in data:
            return  # keine Plan-Nachricht, ignorieren

        try:
            plan_id = read_plan_id(data["plan"])
        except ValueError as exc:
            print(f"[client] {exc}", file=sys.stderr)
            plan_id = None
        plan_queue.put(plan_id)

    client.on_message = on_message
    client.subscribe(env.TOPIC_PLAN, qos=env.QOS)
    print(f"[client] Szenario {replayer.scenario.get('scenario')}, "
          f"{len(replayer.scenario['steps'])} Steps - warte auf den ersten Plan")

    try:
        while state["terminal"] is None:
            try:
                plan_id = plan_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if plan_id is not None and plan_id != last_plan_id:
                # Wechselt die id, hat der Reasoner den Plan gewechselt - genau
                # dafuer haengt sie am Planschluessel und nicht am Eingabeplan.
                how = "Plan" if last_plan_id is None else "neuer Plan"
                print(f"[client] {how} {plan_id}")
                last_plan_id = plan_id
            if state["terminal"] is not None:
                break  # Terminalnachricht kam, waehrend das Token in der Queue lag

            if args.step_delay:
                time.sleep(args.step_delay)
            action_id = replayer.next_action()
            if action_id is None:
                continue  # Datei leergespielt, jetzt auf die Terminalnachricht warten
            print(f"[client] Step {len(replayer.sent):>2} -> {action_id[:12]}")
            client.publish(
                env.TOPIC_ACTION,
                json.dumps(env.action_message(replayer.assembly_id, action_id),
                           ensure_ascii=False),
                qos=env.QOS)
    except KeyboardInterrupt:
        print("\n[client] abgebrochen", file=sys.stderr)
        return 130
    finally:
        client.loop_stop()
        client.disconnect()

    terminal = state["terminal"]
    print(f"[client] Reasoner meldet {terminal['type']} nach {len(replayer.sent)} Steps")
    if terminal.get("reason"):
        print(f"[client] Grund: {terminal['reason']}")
    expected = replayer.scenario.get("expectation", "")
    print(f"[client] erwartet war: {expected}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mocks.assembly_client_mock",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--broker-host", default="localhost")
    parser.add_argument("--broker-port", type=int, default=1883)
    parser.add_argument("--step-delay", type=float, default=0.0,
                        help="Pause vor jedem Step in Sekunden")
    args = parser.parse_args(argv)
    return run_mqtt(Replayer(load_scenario(args.scenario)), args)


if __name__ == "__main__":
    sys.exit(main())
