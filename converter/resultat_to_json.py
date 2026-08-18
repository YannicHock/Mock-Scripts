#!/usr/bin/env python3
"""Konvertiert Solver-Resultatdateien (Resultat.txt-Stil) in ein Assembly-Plan-JSON.

Ohne Annotationen entsteht das einfache Format (wie toycar_simple_output.json,
nur valide): type/assembly/assembleSteps.

Mit einer Annotationsdatei (-n, ASP-Fakten wie Stossecke_Li_Annotationen1.lp)
entsteht das erweiterte Format (wie OR_JSON_FrontBumper_Plan_1.json):
zusaetzlich "components" (aus component(...)-Fakten, Typ aus dem %-Kommentar)
und "parts" (Kabel, Schrauben/Muttern/Scheiben/Pluginnuts als durchnummerierte
Instanzen pro Daimler-Nummer sowie Schraubloecher mit einer symbolischen ID
(Koordinate im position-Feld)).
Die subSteps referenzieren dann die Teil-Instanzen (z.B. N910105006002_3) und
erhalten ein "tool"-Feld (Hand/Screwdriver/Nutrunner/Cleaner/Cutter).

Aus der Plan-Datei wird nur der flache Faktenteil ausgewertet:

    action(<actionType>,"<connection>",<subject>,<object>,<seq>)
    tighten_with("<connection>",<seq>,"<fastener>",<tool>)
    position_with("<connection>",<seq>,"<part>",<tool>)

Formatierte Referenzabschnitte (Connection:/SUB_SEQ-Bloecke) wiederholen die
action-Fakten; Duplikate werden ignoriert, alle anderen Zeilen uebersprungen.
IDs der Schritte sind SHA-256-Hashes ueber den Original-Fakt und aendern sich
durch die Umbenennung unten nicht. Schraubloecher bekommen die symbolische ID
<connection>_screwhole_<n>; die Koordinate bleibt im position-Feld erhalten und
wird auch in den subSteps durch die symbolische ID ersetzt.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

FACT_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*\((.*)\)\s*\.?\s*$")
COORD_RE = re.compile(
    r"^\s*[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
    r"(?:\s*,\s*[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?){2}\s*$"
)

HARDWARE_PREDICATES = {
    "has_screw": "screw",
    "has_nut": "nut",
    "has_washer": "washer",
    "has_pluginnut": "pluginnut",
}

TOOL_BY_ACTION = {"clean": "Cleaner", "cut_to_length": "Cutter"}


def split_args(argstr):
    """Teilt eine Argumentliste an Top-Level-Kommas, Anfuehrungszeichen bleiben intakt."""
    args, buf, depth, in_quotes = [], [], 0, False
    for ch in argstr:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes and depth == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            if not in_quotes:
                if ch in "([":
                    depth += 1
                elif ch in ")]":
                    depth -= 1
            buf.append(ch)
    if buf:
        args.append("".join(buf).strip())
    return args


def norm(value):
    """Entfernt Anfuehrungszeichen; ""/none -> None."""
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = value[1:-1]
    if value == "" or value.lower() == "none":
        return None
    return value


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def sha_id(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def strip_comment(line):
    """Trennt einen %-Kommentar ab (Prozentzeichen in Strings bleiben erhalten)."""
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "%" and not in_quotes:
            return line[:i], line[i + 1 :].strip()
    return line, None


# ---------------------------------------------------------------------------
# Plan (Resultat.txt)
# ---------------------------------------------------------------------------

def parse_facts(text):
    """Liefert deduplizierte Fakten als Liste (name, args, raw) in Dateireihenfolge."""
    facts, seen = [], set()
    for line in text.splitlines():
        match = FACT_RE.match(line)
        if not match:
            continue
        raw = match.group(0).strip()
        if raw in seen:
            continue
        seen.add(raw)
        facts.append((match.group(1), split_args(match.group(2)), raw))
    return facts


def parse_plan(text):
    """Extrahiert assemble-Reihenfolge, Teilschritte und Werkzeuge aus den Fakten."""
    assemble_steps = {}   # connection -> {"seq": int, "raw": str}
    sub_actions = {}      # connection -> list of dicts
    legacy_tools = {}     # (connection, seq) -> tool aus *_with-Fakten

    for name, args, raw in parse_facts(text):
        if name == "action" and len(args) >= 5:
            action_type = norm(args[0])
            connection = norm(args[1])
            seq = as_int(norm(args[4]))
            if action_type == "assemble":
                assemble_steps[connection] = {"seq": seq, "raw": raw}
                continue
            sub_actions.setdefault(connection, []).append(
                {
                    "raw": raw,
                    "actionType": action_type,
                    "subject": norm(args[2]),
                    "object": norm(args[3]),
                    "seq": seq,
                }
            )
        elif name.endswith("_with") and len(args) >= 3:
            key = (norm(args[0]), as_int(norm(args[1])))
            legacy_tools[key] = norm(args[-1])

    next_seq = max((s["seq"] for s in assemble_steps.values()), default=-1) + 1
    for connection in sub_actions:
        if connection not in assemble_steps:
            raw = f'action(assemble,"{connection}",none,none,{next_seq})'
            assemble_steps[connection] = {"seq": next_seq, "raw": raw}
            next_seq += 1

    return assemble_steps, sub_actions, legacy_tools


# ---------------------------------------------------------------------------
# Annotationen (.lp)
# ---------------------------------------------------------------------------

def parse_annotations(text):
    ann = {
        "components": [],      # (id, typ)
        "cables": [],
        "declared": set(),
        "connected": set(),
        "points": {},          # connection -> [coord, ...] in Indexreihenfolge
        "hardware": {},        # coord -> {"screw": id, "nut": id, ...}
    }
    point_order = {}
    for line in text.splitlines():
        code, comment = strip_comment(line)
        match = FACT_RE.match(code)
        if not match:
            continue
        name, args = match.group(1), [norm(a) for a in split_args(match.group(2))]
        if name == "component" and args:
            ann["components"].append((args[0], comment or "none"))
            ann["declared"].add(args[0])
        elif name == "cable" and args:
            ann["cables"].append(args[0])
        elif name == "has_connected_component" and len(args) >= 2:
            ann["connected"].add(args[1])
        elif name == "has_connection_point" and len(args) >= 2:
            idx = as_int(args[2]) if len(args) >= 3 else 0
            point_order.setdefault(args[0], []).append((idx, args[1]))
        elif name in HARDWARE_PREDICATES and len(args) >= 2:
            ann["hardware"].setdefault(args[0], {})[HARDWARE_PREDICATES[name]] = args[1]
    for connection, points in point_order.items():
        ann["points"][connection] = [coord for _, coord in sorted(points)]
    return ann


def catalog_entry(part_type, asp_id, daimler=None, position="none"):
    entry = {"type": part_type, "aspID": asp_id}
    if daimler is not None:
        entry["Daimler"] = daimler
    entry.update({"montavizID": "none", "assistID": "none", "position": position})
    return entry


def build_catalog(ann, connection_order):
    """Baut components/parts-Listen und die Instanz-Zuordnung fuer die subSteps.

    Schrauben/Muttern/Scheiben werden pro Daimler-Nummer durchnummeriert
    (N910105006002_1, _2, ...), in der Reihenfolge der Montageschritte.
    """
    components = [catalog_entry(kind, cid, cid) for cid, kind in ann["components"]]

    parts = [catalog_entry("cable", cable, cable) for cable in ann["cables"]]
    counters = {}          # daimler -> laufende Nummer
    instances = {}         # (coord, daimler) -> instanz-ID
    ordered = list(connection_order) + [
        c for c in ann["points"] if c not in connection_order
    ]
    hole_ids = {}          # (connection, coord) -> symbolische ID
    for connection in ordered:
        number = 0
        for coord in ann["points"].get(connection, []):
            number += 1
            hole_id = f"{connection}_screwhole_{number}"
            hole_ids[(connection, coord)] = hole_id
            hardware = ann["hardware"].get(coord)
            if not hardware:
                continue  # z.B. Klebepunkte ohne Verbindungselemente
            for part_type in ("washer", "screw", "nut", "pluginnut"):
                daimler = hardware.get(part_type)
                if daimler is None:
                    continue
                counters[daimler] = counters.get(daimler, 0) + 1
                instance = f"{daimler}_{counters[daimler]}"
                instances[(coord, daimler)] = instance
                parts.append(catalog_entry(part_type, instance, daimler))
            parts.append(
                catalog_entry("screwhole", hole_id, position=f"POS: {coord}")
            )

    # Fallback fuer Aktionen ohne Koordinate (z.B. insert_pluginnut): eindeutig,
    # wenn es genau eine Instanz der Daimler-Nummer gibt.
    unique = {}
    for (_, daimler), instance in instances.items():
        unique[daimler] = None if daimler in unique else instance
    instances["_unique"] = {d: i for d, i in unique.items() if i}
    return components, parts, instances, hole_ids


def resolve_instance(instances, coord, part):
    if part is None:
        return None
    if coord is not None:
        hit = instances.get((coord, part))
        if hit:
            return hit
    return instances["_unique"].get(part)


def pick_tool(action_type, coord, part, ann):
    if action_type in TOOL_BY_ACTION:
        return TOOL_BY_ACTION[action_type]
    if action_type == "tighten":
        hardware = ann["hardware"].get(coord, {}) if coord else {}
        return "Nutrunner" if hardware.get("nut") == part else "Screwdriver"
    return "Hand"


# ---------------------------------------------------------------------------
# Plan-JSON bauen
# ---------------------------------------------------------------------------

def build_plan(plan_text, assembly_name, plan_id=None, ann=None):
    assemble_steps, sub_actions, legacy_tools = parse_plan(plan_text)
    ordered = sorted(assemble_steps.items(), key=lambda kv: kv[1]["seq"])

    catalog = None
    if ann is not None:
        components, parts, instances, hole_ids = build_catalog(
            ann, [connection for connection, _ in ordered]
        )
        catalog = (components, parts, instances, hole_ids)

    steps = []
    for connection, info in ordered:
        sub_steps = []
        for sub in sorted(sub_actions.get(connection, []), key=lambda s: s["seq"]):
            subject, obj = sub["subject"], sub["object"]
            coord = subject if subject and COORD_RE.match(subject) else None
            entry = {
                "id": sha_id(sub["raw"]),
                "actionType": sub["actionType"],
                "subject": subject,
                "object": obj,
                "step": str(sub["seq"]),
            }
            if ann is not None:
                instance = resolve_instance(catalog[2], coord, obj)
                if instance:
                    entry["object"] = instance
                if coord is not None:
                    hole_id = catalog[3].get((connection, coord))
                    if hole_id is not None:
                        entry["subject"] = hole_id
                entry["tool"] = pick_tool(sub["actionType"], coord, obj, ann)
            else:
                # einfaches Format: Koordinate in eigenes Feld auslagern
                if coord:
                    entry["subject"] = None
                    entry["position"] = coord
                tool = legacy_tools.get((connection, sub["seq"]))
                if tool is not None:
                    entry["tool"] = tool
            sub_steps.append(entry)
        steps.append(
            {
                "id": sha_id(info["raw"]),
                "actionType": "assemble",
                "connection": connection,
                "step": str(info["seq"]),
                "subSteps": sub_steps,
            }
        )

    plan = {"type": "plan"}
    if ann is not None:
        plan["id"] = plan_id or f"{assembly_name}_plan_1"
    plan["assembly"] = assembly_name
    if catalog is not None:
        plan["components"] = catalog[0]
        plan["parts"] = catalog[1]
    plan["assembleSteps"] = steps
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m converter.resultat_to_json",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="Plan-Datei (z.B. Resultat.txt)")
    parser.add_argument(
        "-n", "--annotations", type=Path,
        help="ASP-Annotationsdatei (.lp); aktiviert das erweiterte Format "
             "mit components/parts",
    )
    parser.add_argument(
        "-a", "--assembly",
        help="Assembly-Name im JSON (Default: Dateiname der Eingabedatei ohne Endung)",
    )
    parser.add_argument(
        "--plan-id",
        help="Plan-ID im erweiterten Format (Default: <assembly>_plan_1)",
    )
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Ausgabedatei (Default: <Eingabename>_output.json)",
    )
    args = parser.parse_args(argv)

    plan_text = args.input.read_text(encoding="utf-8")
    assembly_name = args.assembly or args.input.stem
    ann = None
    if args.annotations:
        ann = parse_annotations(args.annotations.read_text(encoding="utf-8"))
        undeclared = sorted(ann["connected"] - ann["declared"] - set(ann["cables"]))
        if undeclared:
            print(
                "Warnung: in Verbindungen referenziert, aber nicht als "
                f"component(...) deklariert: {', '.join(undeclared)}",
                file=sys.stderr,
            )

    plan = build_plan(plan_text, assembly_name, args.plan_id, ann)

    output = args.output or args.input.with_name(args.input.stem + "_output.json")
    output.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    n_subs = sum(len(s["subSteps"]) for s in plan["assembleSteps"])
    summary = f"{output}: {len(plan['assembleSteps'])} assembleSteps, {n_subs} subSteps"
    if ann is not None:
        summary += f", {len(plan['components'])} components, {len(plan['parts'])} parts"
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
