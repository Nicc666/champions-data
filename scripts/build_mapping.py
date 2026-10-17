#!/usr/bin/env python3
"""
Crea mapping.json: collega gli ID di Showdown agli ID del database dell'app.

Si lancia sul tuo PC, solo quando cambia il database (nuovi Pokémon, mosse, strumenti):
    python3 scripts/build_mapping.py percorso/data.db

Legge le tabelle pokemon, moves, items e abilities e scrive mapping.json nella
cartella del repository. build_usage.py lo usa per aggiungere "game_id" ai dati.

- Mosse, strumenti e abilità si collegano per nome inglese ("Grassy Glide" -> grassyglide).
- I Pokémon si collegano dall'id del database con alcune regole per Mega, forme
  regionali e forme "di default" (es. lycanroc_midday su Showdown è solo "Lycanroc").
- Casi nuovi o particolari: aggiungili a mapping_overrides.json, che ha la precedenza.

Alla fine controlla i dati già pubblicati in docs/data e ti dice cosa non è collegato.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Su Showdown la forma base di queste specie non ha suffisso
DEFAULT_FORMS = {
    "aegislash_shield": "aegislash",
    "alcremie_vanilla_cream": "alcremie",
    "basculegion_male": "basculegion",
    "castform_normal": "castform",
    "florges_red_flower": "florges",
    "furfrou_natural": "furfrou",
    "gourgeist_medium_variety": "gourgeist",
    "indeedee_male": "indeedee",
    "lycanroc_midday": "lycanroc",
    "maushold_family_of_three": "maushold",
    "meowstic_male": "meowstic",
    "mimikyu_disguised": "mimikyu",
    "morpeko_full_belly_mode": "morpeko",
    "palafin_zero": "palafin",
    "polteageist_phony": "polteageist",
    "sinistcha_unremarkable": "sinistcha",
    "squawkabilly_green_plumage": "squawkabilly",
    "toxtricity_amped": "toxtricity",
    "vivillon_meadow": "vivillon",
}

# Forme con un nome Showdown che la regola generale non ricava
SPECIAL_FORMS = {
    "basculegion_female": "basculegionf",
    "floette_eternal_flower": "floetteeternal",
    "gourgeist_jumbo_variety": "gourgeistsuper",
    "indeedee_female": "indeedeef",
    "maushold_family_of_four": "mausholdfour",
    "meowstic_female": "meowsticf",
    "meowstic_mega": "meowsticmmega",
    "meowstic_mega_3": "meowsticfmega",
    "morpeko_hangry_mode": "morpekohangry",
}

# Specie base delle Mega quando non si ricava da sola
MEGA_BASE_SPECIAL = {
    "meowstic_mega": "meowstic_male",
    "meowstic_mega_3": "meowstic_female",
}

REGIONAL = {"alolan": "alola", "galarian": "galar", "hisuian": "hisui"}
DROP_SUFFIX = re.compile(r"_(flower|trim|variety|plumage|mode)$")
NO_ITEM = {"nothing", ""}


def to_id(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def showdown_id_for(db_id: str) -> str:
    """Id del database -> id Showdown (es. charizard_mega_y -> charizardmegay)."""
    if db_id in SPECIAL_FORMS:
        return SPECIAL_FORMS[db_id]
    if db_id in DEFAULT_FORMS:
        return DEFAULT_FORMS[db_id]
    m = re.fullmatch(r"(\w+?)_paldean_form_(\w+)_breed", db_id)
    if m:
        return f"{m.group(1)}paldea{m.group(2)}".replace("_", "")
    s = "_".join(REGIONAL.get(part, part) for part in db_id.split("_"))
    s = DROP_SUFFIX.sub("", s)
    return s.replace("_", "")


def mega_base(db_id: str, dex_id: int, ids: set[str], by_dex: dict[int, list[str]]) -> str | None:
    if "_mega" not in db_id:
        return None
    if db_id in MEGA_BASE_SPECIAL:
        return MEGA_BASE_SPECIAL[db_id]
    candidate = db_id.split("_mega")[0]
    if candidate in ids:
        return candidate
    others = [i for i in by_dex[dex_id] if "_mega" not in i]
    return others[0] if len(others) == 1 else None


def load_overrides(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build(db_path: Path, overrides: dict) -> tuple[dict, list[str]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    problems: list[str] = []

    rows = con.execute("SELECT id, dex_id FROM pokemon").fetchall()
    ids = {r[0] for r in rows}
    by_dex: dict[int, list[str]] = defaultdict(list)
    for db_id, dex in rows:
        by_dex[dex].append(db_id)

    pokemon: dict[str, str] = {}
    bases: dict[str, str] = {}
    for db_id, dex in sorted(rows):
        sid = showdown_id_for(db_id)
        if sid in pokemon and pokemon[sid] != db_id:
            problems.append(f"Pokémon: '{sid}' corrisponde sia a {pokemon[sid]} sia a {db_id}")
        pokemon[sid] = db_id
        base = mega_base(db_id, dex, ids, by_dex)
        if "_mega" in db_id:
            if base:
                bases[db_id] = base
            else:
                problems.append(f"Mega senza specie base: {db_id} (aggiungila in mapping_overrides.json, sezione mega_base)")

    tables = {}
    for table in ("moves", "items", "abilities"):
        index: dict[str, int] = {}
        for row_id, name in con.execute(f"SELECT id, name FROM {table}"):
            key = to_id(name or "")
            if not key:
                continue
            if key in index and index[key] != row_id:
                problems.append(f"{table}: '{name}' compare due volte (id {index[key]} e {row_id})")
            index[key] = row_id
        tables[table] = index
    con.close()

    pokemon.update(overrides.get("pokemon", {}))
    bases.update(overrides.get("mega_base", {}))
    for table in tables:
        tables[table].update(overrides.get(table, {}))

    mapping = {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "pokemon": dict(sorted(pokemon.items())),
        "mega_base": dict(sorted(bases.items())),
        "moves": dict(sorted(tables["moves"].items())),
        "items": dict(sorted(tables["items"].items())),
        "abilities": dict(sorted(tables["abilities"].items())),
    }
    return mapping, problems


def coverage(mapping: dict, data_dir: Path, names_file: Path | None) -> dict[str, set[str]]:
    """ID presenti nei dati pubblicati (e nell'eventuale elenco di nomi) che non sono collegati."""
    missing: dict[str, set[str]] = defaultdict(set)
    for f in sorted(data_dir.glob("*/*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for p in data.get("pokemon", []):
            if p["id"] not in mapping["pokemon"]:
                missing["pokemon"].add(p["name"])
            for t in p.get("teammates", []):
                if t["id"] not in mapping["pokemon"]:
                    missing["pokemon"].add(t["name"])
            for sec in ("moves", "items", "abilities"):
                for e in p.get(sec, []):
                    if e["id"] not in mapping[sec] and e["id"] not in NO_ITEM:
                        missing[sec].add(e["id"])
    if names_file:
        for line in names_file.read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if name and to_id(name) not in mapping["pokemon"]:
                missing["pokemon"].add(name)
    return missing


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Genera mapping.json dal database dell'app")
    p.add_argument("db", help="percorso del file data.db")
    p.add_argument("--out", default=str(ROOT / "mapping.json"))
    p.add_argument("--overrides", default=str(ROOT / "mapping_overrides.json"))
    p.add_argument("--data", default=str(ROOT / "docs" / "data"),
                   help="cartella con i dati pubblicati da controllare")
    p.add_argument("--names", help="file di testo con nomi Showdown, uno per riga, da controllare")
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERRORE: database non trovato: {db_path}")
        return 1

    mapping, problems = build(db_path, load_overrides(Path(args.overrides)))
    out = Path(args.out)
    out.write_text(json.dumps(mapping, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Scritto {out.name}: {len(mapping['pokemon'])} Pokémon, {len(mapping['moves'])} mosse, "
          f"{len(mapping['items'])} strumenti, {len(mapping['abilities'])} abilità, "
          f"{len(mapping['mega_base'])} Mega con specie base")

    for msg in problems:
        print(f"ATTENZIONE: {msg}")

    missing = coverage(mapping, Path(args.data), Path(args.names) if args.names else None)
    if any(missing.values()):
        print("\nNon collegati (aggiungili a mapping_overrides.json se esistono nel tuo database):")
        for sec, values in missing.items():
            if values:
                print(f"  {sec}: {', '.join(sorted(values))}")
    else:
        print("Tutto collegato.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
