#!/usr/bin/env python3
"""Operation-Reasoner-Mock.

Spannt aus einem Montageplan den Raum aller zulaessigen Ausfuehrungsreihenfolgen
auf, schickt einen davon ueber MQTT an den Client und grenzt den Raum mit jedem
gemeldeten Arbeitsschritt weiter ein.

Der Mock simuliert kein Reasoning - er permutiert und filtert. Die
Zustandsmaschine (Klasse Reasoner) kennt kein MQTT und ist ohne Broker testbar.
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

from . import mqtt_envelope as env
from . import plan_space


class Reasoner:
    """Zustandsmaschine: beobachtete Steps rein, zu sendende Nachricht raus."""

    def __init__(self, space, assembly_id, rng):
        self.space = space
        self.assembly_id = assembly_id
        self.rng = rng
        self.observed = []
        self.current = None
        self.finished = False
        self.last_match_count = None
        # Die getroffene Entscheidung, damit der Aufrufer sie ausgeben kann,
        # statt sie aus dem Wechsel von self.current zu erraten:
        # "chosen" | "kept" | "switched" | "complete" | "nomatch"
        self.decision = None
        self.plan_number = 0
        self.switches = 0
        self.total = sum(len(b["subSteps"]) for b in space.blocks)

    def start(self):
        """Waehlt zufaellig einen Plan und liefert die zu sendende Nachricht."""
        self.current = self.space.sample_matching((), self.rng)
        self.plan_number = 1
        self.decision = "chosen"
        return env.plan_message(self.space.plan_json(self.current))

    def on_action(self, action_id):
        """Verarbeitet einen gemeldeten Step und liefert die Antwortnachricht."""
        if self.finished:
            raise RuntimeError("Reasoner ist bereits beendet")

        self.observed.append(action_id)
        prefix = tuple(self.observed)
        self.last_match_count = self.space.count_matching(prefix)

        if self.last_match_count == 0:
            self.finished = True
            self.decision = "nomatch"
            return env.terminal_message(
                "noMatchingPlan", self.assembly_id, self.observed,
                reason=self._reason(action_id))

        if len(self.observed) == self.total:
            self.finished = True
            self.decision = "complete"
            return env.terminal_message("planComplete", self.assembly_id, self.observed)

        if self.current is None or not self.space.matches(self.current, prefix):
            self.current = self.space.sample_matching(prefix, self.rng)
            self.plan_number += 1
            self.switches += 1
            self.decision = "switched"
        else:
            self.decision = "kept"
        return env.plan_message(self.space.plan_json(self.current))

    def _reason(self, action_id):
        known = any(s["id"] == action_id
                    for b in self.space.blocks for s in b["subSteps"])
        if not known:
            return f"unbekannte actionID {action_id}"
        if len(self.observed) > 1 and action_id in self.observed[:-1]:
            return f"actionID {action_id} wurde bereits gemeldet"
        return (f"kein Plan hat diese Reihenfolge - vermutlich wurde ein "
                f"assembleStep verlassen, bevor er fertig war "
                f"(Step {len(self.observed)}: {action_id[:12]})")


def describe_key(key):
    """Kurzbeschreibung eines Planschluessels fuer die Logausgabe."""
    return ("Bloecke " + "-".join(str(b) for b in key.order)
            + ", Layouts " + "/".join(str(n) for n in key.layouts))


def build_space(plan_path):
    plan = plan_space.strip_null_subjects(plan_space.load_plan(plan_path))
    return plan_space.PlanSpace(plan), plan["assembly"]


def run_mqtt(space, assembly_id, args):
    """Faehrt die Zustandsmaschine ueber MQTT, bis sie sich beendet."""
    reasoner = Reasoner(space, assembly_id,
                        random.Random(args.seed) if args.seed is not None
                        else random.Random())
    client = env.connect(args.broker_host, args.broker_port, "or-reasoner-mock")

    total = space.count()
    width = len(str(total))

    def decision_text():
        """Was die Zustandsmaschine gerade entschieden hat, in einem Satzteil."""
        if reasoner.decision == "kept":
            return f"Plan {reasoner.plan_number} passt weiter"
        if reasoner.decision == "switched":
            return (f"Plan {reasoner.plan_number - 1} passt nicht mehr"
                    f" -> Plan {reasoner.plan_number}: {describe_key(reasoner.current)}")
        if reasoner.decision == "complete":
            return f"alle {reasoner.total} Substeps gemeldet -> planComplete"
        return "kein Plan passt mehr -> noMatchingPlan"

    def publish(message):
        client.publish(env.TOPIC_PLAN, json.dumps(message, ensure_ascii=False),
                       qos=env.QOS)

    def on_message(_client, _userdata, msg):
        try:
            data = env.read_data(msg.payload)
        except ValueError as exc:
            print(f"[reasoner] verworfen: {exc}", file=sys.stderr)
            return
        action_id = data.get("actionID")
        if action_id is None:
            print("[reasoner] verworfen: Nachricht ohne actionID", file=sys.stderr)
            return
        if reasoner.finished:
            return
        response = reasoner.on_action(action_id)
        print(f"[reasoner] Step {len(reasoner.observed):>2} {action_id[:12]}  "
              f"{reasoner.last_match_count:>{width}} von {total} Plaene passen"
              f"  {decision_text()}")
        publish(response)

    client.on_message = on_message
    client.subscribe(env.TOPIC_ACTION, qos=env.QOS)

    print(f"[reasoner] Planraum: {total} Plaene aus {len(space.blocks)} assembleSteps,"
          f" {reasoner.total} Substeps")
    first = reasoner.start()
    print(f"[reasoner] Plan 1 zufaellig gewaehlt: {describe_key(reasoner.current)}")
    publish(first)

    try:
        while not reasoner.finished:
            time.sleep(0.05)
        time.sleep(0.5)  # ausgehende Nachricht noch rausschreiben lassen
    except KeyboardInterrupt:
        print("\n[reasoner] abgebrochen", file=sys.stderr)
        return 130
    finally:
        client.loop_stop()
        client.disconnect()

    kind = "planComplete" if reasoner.decision == "complete" else "noMatchingPlan"
    print(f"[reasoner] Ende: {kind} nach {len(reasoner.observed)} Steps,"
          f" {reasoner.switches} Planwechsel")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mocks.or_reasoner_mock",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--plan", type=Path,
                        default=Path("data/plans/test_plan_1_3.json"),
                        help="Eingabeplan")
    parser.add_argument("--broker-host", default="localhost")
    parser.add_argument("--broker-port", type=int, default=1883)
    parser.add_argument("--store", choices=("index", "plans"), default="index",
                        help="Ablageform der erzeugten Plaene")
    parser.add_argument("--out-dir", type=Path, default=Path("generated"))
    parser.add_argument("--seed", type=int, help="reproduzierbare Planauswahl")
    parser.add_argument("--emit-scenarios", type=Path, metavar="DIR",
                        help="Szenariodateien erzeugen und beenden, ohne MQTT")
    parser.add_argument("--max-store-count", type=int,
                        default=plan_space.DEFAULT_STORE_CEILING,
                        help="Obergrenze fuer die Planzahl bei --store "
                             "index/plans; darueber wird die Ablage "
                             "verweigert statt das Dateisystem zu fluten "
                             "(Default %(default)s)")
    args = parser.parse_args(argv)

    space, assembly_id = build_space(args.plan)

    if args.emit_scenarios:
        paths = plan_space.write_scenarios(space, assembly_id, args.emit_scenarios)
        for p in paths:
            print(p)
        return 0

    # Finding 1: write_index/write_plans iterieren jeden Schluessel einzeln -
    # ohne diesen Check wuerde ein Plan wie Resultat_output_2.json (385
    # Billiarden Plaene) das Dateisystem stumm fluten, bis die Platte voll
    # ist. count() ist dagegen guenstig (Produkt aus Fakultaet und
    # Layoutzahlen, keine Enumeration).
    count = space.count()
    if count > args.max_store_count:
        print(f"[reasoner] {args.plan} hat {count} Plaene - das liegt ueber "
              f"der Ablage-Obergrenze {args.max_store_count} fuer "
              f"--store {args.store}. Es wird nichts geschrieben und der "
              f"Reasoner startet nicht. Mit --max-store-count <n> laesst "
              f"sich die Grenze bewusst anheben, wenn die Ablage wirklich "
              f"gewollt ist.", file=sys.stderr)
        return 1

    if args.store == "index":
        base, index = plan_space.write_index(space, args.out_dir)
        print(f"[reasoner] {base}, {index} ({index.stat().st_size / 1e6:.1f} MB)")
    else:
        path = plan_space.write_plans(space, args.out_dir)
        print(f"[reasoner] {path} ({path.stat().st_size / 1e6:.1f} MB)")

    return run_mqtt(space, assembly_id, args)


if __name__ == "__main__":
    sys.exit(main())
