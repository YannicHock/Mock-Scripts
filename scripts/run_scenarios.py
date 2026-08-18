#!/usr/bin/env python3
"""Faehrt alle Szenarien nacheinander gegen den laufenden Broker.

Startet je Szenario einen Reasoner und einen Client als Unterprozesse und prueft,
mit welcher Terminalnachricht der Lauf endet. Setzt voraus, dass der Broker
laeuft (docker compose -f docker/mosquitto/compose.yaml up -d).

Standardmaessig als Demo: die Ausgabe beider Prozesse wird live und ineinander
verschraenkt durchgereicht, damit der Dialog nachvollziehbar ist. --quiet
reduziert auf je eine Ergebniszeile, wie es fuer einen CI-Lauf reicht.
"""

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

TIMEOUT = 60

# Zeile, die der Client druckt, sobald er verbunden und abonniert hat (siehe
# assembly_client_mock.py). Ersetzt das reine Warten per sleep() als
# Startsignal fuer den Reasoner.
CLIENT_READY_MARKER = "warte auf den ersten Plan"
CLIENT_READY_TIMEOUT = 15

# 07_early_stop hat keine Terminalnachricht: beide Prozesse muessen am Leben
# bleiben. So lange wird dafuer beobachtet.
IDLE_OBSERVATION = 25

RULE = "-" * 78
PLAN_SPACE_RE = re.compile(r"Planraum: (\d+) Plaene")
MATCH_COUNT_RE = re.compile(r"(\d+) von \d+ Plaene passen")
SWITCH_RE = re.compile(r"(\d+) Planwechsel")

# Eine Sperre fuer alle Ausgaben: die beiden Pump-Threads und der Hauptthread
# schreiben sonst mitten in fremde Zeilen hinein.
_print_lock = threading.Lock()


def emit(text=""):
    with _print_lock:
        print(text, flush=True)


def _pump(pipe, buffer, echo, ready_event=None, marker=None):
    """Liest Zeilen aus pipe bis zum Prozessende und sammelt sie in buffer.

    Laeuft in einem eigenen Thread. Zwei Gruende: auf die Bereitschaftszeile des
    Clients kann gewartet werden, ohne dass die spaeter benoetigte komplette
    Ausgabe verlorengeht, und beide Pipes werden von Anfang an entwaessert statt
    eine davon erst am Ende - sonst liefe sie im Leerlaufszenario voll.

    Mit echo=True wird jede Zeile sofort ausgegeben; nur so ist der Dialog als
    Demo lesbar. Das Padding gleicht die unterschiedlich langen Prefixe der
    beiden Mocks aus, damit die Spalten stehen.
    """
    for line in iter(pipe.readline, ""):
        buffer.append(line)
        if ready_event is not None and not ready_event.is_set() and marker in line:
            ready_event.set()
        if echo:
            shown = line.rstrip()
            if shown.startswith("[client]"):
                shown = "[client]  " + shown[len("[client]"):]
            emit("      " + shown)
    pipe.close()


def _terminate(*procs):
    """Beendet alle uebergebenen (ggf. None-)Subprozesse zuverlaessig."""
    for p in procs:
        if p is not None and p.poll() is None:
            p.kill()
    for p in procs:
        if p is not None:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass


def summarise_steps(steps):
    """Fasst die Schrittfolge nach assembleStep und Aktionstyp zusammen.

    Aus 23 einzelnen actionIDs werden vier Zeilen, die die Blockstruktur zeigen -
    genau die Struktur, auf der das Matching beruht. Bei 04_block_jump wird
    daran unmittelbar sichtbar, dass der erste Block unvollstaendig bleibt.
    """
    groups = []
    for step in steps:
        connection = step.get("_connection", "?")
        kind = step.get("_actionType", "?")
        if groups and groups[-1][0] == connection:
            groups[-1][1].append(kind)
        else:
            groups.append((connection, [kind]))

    lines = []
    for connection, kinds in groups:
        parts, i = [], 0
        while i < len(kinds):
            j = i
            while j < len(kinds) and kinds[j] == kinds[i]:
                j += 1
            parts.append(kinds[i] if j - i == 1 else f"{kinds[i]} x{j - i}")
            i = j
        lines.append((connection, ", ".join(parts)))
    return lines


def narrowing(reasoner_out):
    """Liefert 'erste -> letzte' Zahl passender Plaene, sofern beide im Log stehen."""
    total = PLAN_SPACE_RE.search(reasoner_out)
    counts = MATCH_COUNT_RE.findall(reasoner_out)
    if not total or not counts:
        return None
    return f"{total.group(1)} -> {counts[-1]}"


def run_scenario(path, scenario, expected, echo):
    """Faehrt ein Szenario und liefert (ok, kurzbeschreibung, dauer)."""
    started = time.perf_counter()

    # Der Client MUSS zuerst laufen: der Reasoner published seine erste
    # Plan-Nachricht sofort, und ohne Retain ist sie verloren, wenn noch
    # niemand abonniert hat.
    # -u: unbuffered stdout. Ohne Flag puffert Python print() blockweise,
    # sobald stdout keine Konsole ist (also auch als Pipe hier) - die
    # Bereitschaftszeile wuerde sonst erst beim Prozessende ankommen.
    client = subprocess.Popen(
        [sys.executable, "-u", "-m", "mocks.assembly_client_mock",
         "--scenario", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    reasoner = None
    client_lines, reasoner_lines = [], []
    client_ready = threading.Event()
    readers = [threading.Thread(
        target=_pump,
        args=(client.stdout, client_lines, echo, client_ready, CLIENT_READY_MARKER),
        daemon=True)]
    readers[0].start()

    def done(ok, detail):
        return ok, detail, time.perf_counter() - started

    try:
        if not client_ready.wait(timeout=CLIENT_READY_TIMEOUT):
            return done(False, f"Client hat sich nicht innerhalb von "
                               f"{CLIENT_READY_TIMEOUT}s bereit gemeldet")

        reasoner = subprocess.Popen(
            [sys.executable, "-u", "-m", "mocks.or_reasoner_mock", "--seed", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        readers.append(threading.Thread(
            target=_pump, args=(reasoner.stdout, reasoner_lines, echo), daemon=True))
        readers[1].start()

        if expected is None:
            # 07_early_stop: der Client spielt seine halbe Folge ab und wartet
            # dann, der Reasoner wartet ebenfalls. Beide muessen am Leben
            # bleiben. Grosszuegig warten - der Reasoner baut erst den Planraum
            # auf und schreibt den Index, bevor er die erste Nachricht sendet.
            time.sleep(IDLE_OBSERVATION)
            alive = reasoner.poll() is None and client.poll() is None
            return done(alive, "beide laufen weiter" if alive
                               else "einer hat sich beendet")

        try:
            reasoner.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return done(False, f"Timeout nach {TIMEOUT}s")

        for reader in readers:
            reader.join(timeout=TIMEOUT)
            if reader.is_alive():
                return done(False, f"Timeout nach {TIMEOUT}s")
        client_out = "".join(client_lines)
        reasoner_out = "".join(reasoner_lines)

        # Auf die konkreten Logzeilen pruefen, nicht auf das blosse Vorkommen
        # des Wortes: der Client druckt auch "erwartet war: <expectation>",
        # und in Szenario 01/02/03 enthaelt dieser Text bereits
        # "planComplete" - die Pruefung waere sonst teilweise selbsterfuellend.
        ok = (f"Reasoner meldet {expected}" in client_out
              and f"-> {expected}" in reasoner_out)

        detail = expected
        steps = [line for line in client_out.splitlines() if "Reasoner meldet" in line]
        if steps:
            detail = steps[-1].split("meldet", 1)[1].strip()
        space = narrowing(reasoner_out)
        if space:
            detail += f", Planraum {space}"
        switches = SWITCH_RE.search(reasoner_out)
        if switches:
            detail += f", {switches.group(1)} Planwechsel"
        return done(ok, detail)
    finally:
        _terminate(client, reasoner)
        for reader in readers:
            reader.join(timeout=5)


def print_header(index, count, name, scenario, expected):
    emit()
    emit(RULE)
    emit(f" {index}/{count}  {name}")
    description = scenario.get("description")
    if description:
        emit(f"       {description}")
    prose = scenario.get("expectation", "")
    goal = expected if expected else "keine Terminalnachricht"
    emit(f"       erwartet: {goal}" + (f" -- {prose}" if prose != expected else ""))
    emit(RULE)

    steps = scenario.get("steps", [])
    emit(f"      Der Client meldet {len(steps)} Schritte:")
    for connection, kinds in summarise_steps(steps):
        emit(f"        {connection:<26} {kinds}")
    emit()


def print_intro(count):
    emit("=" * 78)
    emit(" MQTT-Mock-Demo   Operation Reasoner  <->  Assembly Client")
    emit("=" * 78)
    emit()
    emit(" Der Reasoner spannt aus dem Montageplan alle gleichwertigen")
    emit(" Ausfuehrungsreihenfolgen auf, schickt eine davon an den Client und")
    emit(" grenzt den Raum mit jedem gemeldeten Schritt weiter ein. Der Client")
    emit(" spielt eine feste Schrittfolge ab - auch solche, die zu keinem Plan")
    emit(" passen.")
    emit()
    emit(f" {count} Szenarien. Achte auf die Zahl der noch passenden Plaene:")
    emit(" sie ist das Mass dafuer, wie weit der Reasoner sich festgelegt hat.")
    emit()
    emit(" Broker: 127.0.0.1:1883")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python scripts/run_scenarios.py",
        description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true",
                        help="nur je eine Ergebniszeile statt des vollen Dialogs")
    parser.add_argument("--scenarios", type=Path, default=Path("scenarios"),
                        help="Verzeichnis mit den Szenariodateien")
    args = parser.parse_args(argv)
    echo = not args.quiet

    # Liest die erwartete Terminalnachricht aus dem `terminal`-Feld jeder
    # Szenariodatei selbst, statt aus einer eigenen Tabelle: die Dateien sind
    # laut Spec 8.1 von Hand editierbar, und ein hartkodiertes EXPECTED wuerde
    # nach einer Handbearbeitung die alte Erwartung pruefen bzw. eine
    # handhinzugefuegte achte Datei nie ausfuehren.
    hint = "erst 'python -m mocks.or_reasoner_mock --emit-scenarios scenarios'"
    if not args.scenarios.is_dir():
        print(f"{args.scenarios}/ fehlt - {hint}")
        return 1
    paths = sorted(args.scenarios.glob("*.json"))
    if not paths:
        print(f"{args.scenarios}/ ist leer - {hint}")
        return 1

    if echo:
        print_intro(len(paths))

    results = []
    for index, path in enumerate(paths, start=1):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        name = scenario.get("scenario", path.stem)
        if "terminal" not in scenario:
            results.append((False, name, f"kein terminal-Feld in {path}", 0.0))
            if echo:
                emit()
                emit(f" {index}/{len(paths)}  {name}: FEHLT, kein terminal-Feld")
            continue

        expected = scenario["terminal"]
        if echo:
            print_header(index, len(paths), name, scenario, expected)
        ok, detail, seconds = run_scenario(path, scenario, expected, echo)
        results.append((ok, name, detail, seconds))
        if echo:
            emit()
            emit(f"      {'OK' if ok else 'FEHLER'}   {detail}   ({seconds:.1f}s)")

    failures = sum(1 for ok, *_ in results if not ok)
    emit()
    emit(RULE)
    emit(" Ergebnis")
    emit(RULE)
    for ok, name, detail, seconds in results:
        emit(f" {'OK    ' if ok else 'FEHLER'}  {name:<26} {detail}"
             f"{'' if seconds == 0 else f'   ({seconds:.1f}s)'}")
    emit()
    emit(f" {len(results) - failures}/{len(results)} Szenarien wie erwartet")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
