#!/usr/bin/env python3
"""
Scarica le statistiche mensili di Smogon (Pokémon Showdown) per Pokémon Champions
e produce file JSON leggeri, pronti per essere letti dall'app.

Uso:
    python3 scripts/build_usage.py              # aggiorna solo se c'è un mese nuovo
    python3 scripts/build_usage.py --force      # rigenera anche se il mese c'è già
    python3 scripts/build_usage.py --month 2026-09

Usa solo la libreria standard di Python: non serve installare nulla.

Cosa fa, in ordine:
  1. legge l'indice di smogon.com/stats e trova il mese più recente;
  2. per ogni dataset attivo in config.json trova da solo il formato giusto
     (es. ...regmc...; quando uscirà ...regmd... lo prende in automatico);
  3. scarica il file "chaos" (JSON completo) e lo riduce: percentuali già calcolate,
     solo i primi N valori per sezione;
  4. confronta i propri calcoli con i file di testo pubblicati da Smogon: se non
     coincidono si ferma senza scrivere nulla;
  5. scrive docs/data/<dataset>/<mese>.json e docs/latest.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://www.smogon.com/stats/"
USER_AGENT = "champions-data-updater/1.0 (GitHub Actions)"
SCHEMA_VERSION = 1

SOURCE = {
    "name": "Smogon / Pokémon Showdown",
    "url": "https://www.smogon.com/stats/",
    "note": "Statistiche mensili del ladder di Pokémon Showdown (simulatore), "
            "non del ranked in-game.",
}

# Sezioni del file chaos che finiscono nel JSON finale (chiave chaos -> chiave output)
SECTIONS = {
    "Abilities": "abilities",
    "Items": "items",
    "Moves": "moves",
    "Spreads": "spreads",
    "Teammates": "teammates",
}
# Sezioni confrontate con il file di testo "moveset" di Smogon
CHECKED_SECTIONS = ("Abilities", "Items", "Moves", "Teammates")
# "Nessuno strumento": non va collegato a niente
NO_ITEM = {"nothing"}


class BuildError(Exception):
    """Errore che deve fermare l'aggiornamento (la Action risulterà fallita)."""


def log(msg: str = "") -> None:
    print(msg, flush=True)


def to_id(text: str) -> str:
    """Stesso ID usato da Showdown: minuscolo, solo lettere e numeri.

    Esempi: "Grassy Glide" -> "grassyglide", "Indeedee-F" -> "indeedeef".
    Applicando la stessa funzione ai nomi inglesi del tuo database ottieni
    la chiave per collegarli ai dati Smogon.
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


def regulation_label(reg: str) -> str:
    """"mc" -> "M-C"."""
    if not reg:
        return ""
    return f"{reg[0].upper()}-{reg[1:].upper()}"


# --------------------------------------------------------------------------- download

def fetch(url: str, retries: int = 3) -> bytes:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise BuildError(f"File non trovato: {url}") from e
            last_err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
        if attempt < retries:
            time.sleep(5 * attempt)
    raise BuildError(f"Download fallito dopo {retries} tentativi: {url} ({last_err})")


def fetch_text(url: str) -> str:
    return fetch(url).decode("utf-8", errors="replace")


def fetch_json(url: str) -> dict:
    try:
        return json.loads(fetch(url))
    except json.JSONDecodeError as e:
        raise BuildError(f"JSON non valido: {url} ({e})") from e


# --------------------------------------------------------------------------- indice Smogon

def list_months(base: str) -> list[str]:
    html = fetch_text(base)
    # solo cartelle AAAA-MM: esclude quelle con suffisso tipo 2023-09-DLC1
    months = sorted(set(re.findall(r'href="(?:[^"]*/)?(\d{4}-\d{2})/"', html)))
    if not months:
        raise BuildError(f"Nessuna cartella mensile trovata in {base}")
    return months


def list_formats(base: str, month: str) -> dict[str, set[int]]:
    """Formati disponibili nel mese, con le fasce di rating (0, 1500, 1630, 1760...)."""
    html = fetch_text(f"{base}{month}/")
    formats: dict[str, set[int]] = {}
    for name, rating in re.findall(r'href="(?:[^"]*/)?([a-z0-9]+)-(\d+)\.txt"', html):
        formats.setdefault(name, set()).add(int(rating))
    if not formats:
        raise BuildError(f"Nessun file di statistiche trovato in {base}{month}/")
    return formats


# --------------------------------------------------------------------------- file di testo Smogon

def parse_total_battles(usage_txt: str) -> int:
    m = re.search(r"Total battles:\s*(\d+)", usage_txt)
    return int(m.group(1)) if m else 0


def parse_usage_table(usage_txt: str) -> dict[str, float]:
    """Righe "| 1 | Rillaboom | 54.09664% | ..." -> {"Rillaboom": 54.09664}."""
    rows: dict[str, float] = {}
    for line in usage_txt.splitlines():
        m = re.match(r"\s*\|\s*\d+\s*\|\s*(.+?)\s*\|\s*(\d+(?:\.\d+)?)%", line)
        if m:
            rows[m.group(1)] = float(m.group(2))
    return rows


def parse_moveset(moveset_txt: str) -> dict[str, dict[str, dict[str, float]]]:
    """Legge il file moveset/*.txt: {Pokémon: {Sezione: {nome: percentuale}}}."""
    lines = [line.strip() for line in moveset_txt.splitlines()]
    blocks: dict[str, dict[str, dict[str, float]]] = {}
    current: dict[str, dict[str, float]] | None = None
    section: dict[str, float] | None = None
    expect_title = False
    entry_re = re.compile(r"^\|\s*(.+?)\s+(-?\d+(?:\.\d+)?)%\s*\|$")

    for i, line in enumerate(lines):
        if line.startswith("| Raw count:") and i >= 2:
            name = lines[i - 2].strip("|").strip()
            current = blocks.setdefault(name, {})
            section = None
            expect_title = False
            continue
        if current is None:
            continue
        if line.startswith("+"):
            section = None
            expect_title = True
            continue
        if expect_title:
            title = line.strip("|").strip()
            section = current.setdefault(title, {}) if title in SECTIONS else None
            expect_title = False
            continue
        if section is not None:
            m = entry_re.match(line)
            if m:
                section[m.group(1)] = float(m.group(2))
    return blocks


# --------------------------------------------------------------------------- scelta del formato

def choose_format(base: str, month: str, formats: dict[str, set[int]],
                  key: str, conf: dict) -> dict:
    pattern = re.compile(conf["pattern"])
    override = conf.get("override")
    rating_pref = int(conf.get("rating", 1760))
    min_battles = int(conf.get("min_battles", 0))

    candidates = []
    for name in formats:
        if override:
            if name != override:
                continue
            m = pattern.fullmatch(name)
            gd = m.groupdict() if m else {}
        else:
            m = pattern.fullmatch(name)
            if not m:
                continue
            gd = m.groupdict()
        candidates.append((int(gd.get("year") or 0), gd.get("reg") or "", name))

    if not candidates:
        what = f"il formato forzato '{override}'" if override else f"formati che corrispondono a {conf['pattern']}"
        raise BuildError(f"[{key}] Nessun file per {what} nel mese {month}. "
                         f"Controlla config.json (forse Smogon ha cambiato nome al formato).")

    # dal regolamento più recente al più vecchio (prima l'anno, poi la lettera)
    candidates.sort(reverse=True)
    checked = []
    for year, reg, name in candidates:
        ratings = formats[name]
        rating = rating_pref if rating_pref in ratings else max(ratings)
        usage_txt = fetch_text(f"{base}{month}/{name}-{rating}.txt")
        battles = parse_total_battles(usage_txt)
        info = {"format": name, "reg": reg, "rating": rating,
                "battles": battles, "usage_txt": usage_txt}
        checked.append(info)
        log(f"  candidato {name}-{rating}: {battles:,} battaglie".replace(",", "."))
        if override or battles >= min_battles:
            return info

    best = max(checked, key=lambda c: c["battles"])
    log(f"  nessun formato supera la soglia di {min_battles} battaglie: "
        f"uso {best['format']} che ne ha di più")
    return best


# --------------------------------------------------------------------------- elaborazione

def section_total(entry: dict) -> float:
    """Peso totale del Pokémon: ogni squadra ha esattamente un'abilità."""
    total = sum(entry.get("Abilities", {}).values())
    if total <= 0:
        total = sum(entry.get("Items", {}).values())
    return total


def top_entries(section: dict, total: float, top: int, min_pct: float) -> list[tuple[str, float]]:
    rows = [(k, v / total * 100) for k, v in section.items() if k and v > 0]
    rows.sort(key=lambda kv: kv[1], reverse=True)
    out = []
    for k, pct in rows[:top]:
        if pct < min_pct:
            break
        out.append((k, pct))
    return out


class Linker:
    """Aggiunge gli ID del database dell'app usando mapping.json (se c'è)."""

    def __init__(self, mapping: dict | None):
        self.mapping = mapping
        self.unmapped: dict[str, set[str]] = {}

    @property
    def active(self) -> bool:
        return self.mapping is not None

    def game_id(self, section: str, showdown_id: str, label: str):
        if self.mapping is None:
            return None
        value = self.mapping.get(section, {}).get(showdown_id)
        if value is None and not (section == "items" and showdown_id in NO_ITEM):
            self.unmapped.setdefault(section, set()).add(label)
        return value

    def mega_base(self, game_id) -> str | None:
        if self.mapping is None or game_id is None:
            return None
        return self.mapping.get("mega_base", {}).get(game_id)

    def report(self) -> dict[str, list[str]]:
        return {k: sorted(v) for k, v in sorted(self.unmapped.items()) if v}


def build_dataset(chaos: dict, conf: dict, info: dict, month: str,
                  linker: Linker | None = None) -> dict:
    linker = linker or Linker(None)
    data = chaos.get("data") or {}
    meta = chaos.get("info") or {}
    if not data:
        raise BuildError(f"Il file chaos di {info['format']} non contiene dati")

    top = conf["top"]
    min_usage = float(conf["min_usage_pct"])
    min_entry = float(conf["min_entry_pct"])

    def ref(section: str, key: str, label: str | None = None) -> dict:
        sid = to_id(key)
        out = {"id": sid}
        if linker.active:
            out["game_id"] = linker.game_id(section, sid, label or key)
        return out

    pokemon = []
    for name, d in data.items():
        usage = float(d.get("usage") or 0) * 100
        if usage < min_usage:
            continue
        total = section_total(d)
        if total <= 0:
            continue
        entry: dict = {"name": name, **ref("pokemon", name)}
        base = linker.mega_base(entry.get("game_id"))
        if base:
            entry["base_game_id"] = base
        entry["usage"] = round(usage, 2)
        entry["raw_count"] = int(d.get("Raw count") or 0)
        for chaos_key, out_key in SECTIONS.items():
            rows = top_entries(d.get(chaos_key) or {}, total, int(top[out_key]), min_entry)
            if out_key == "spreads":
                entry[out_key] = [{"spread": k, "pct": round(p, 1)} for k, p in rows]
            elif out_key == "teammates":
                entry[out_key] = [{"name": k, **ref("pokemon", k), "pct": round(p, 1)} for k, p in rows]
            else:
                entry[out_key] = [{**ref(out_key, k), "pct": round(p, 1)} for k, p in rows]
        pokemon.append(entry)

    pokemon.sort(key=lambda p: p["usage"], reverse=True)
    pokemon = [{"rank": i, **p} for i, p in enumerate(pokemon, start=1)]
    if len(pokemon) < 10:
        raise BuildError(f"Solo {len(pokemon)} Pokémon sopra la soglia: dati sospetti, mi fermo")

    return {
        "schema": SCHEMA_VERSION,
        "month": month,
        "format": info["format"],
        "regulation": regulation_label(info["reg"]),
        "rating": info["rating"],
        "battles": int(meta.get("number of battles") or info["battles"]),
        "source": SOURCE,
        "pokemon": pokemon,
    }


# --------------------------------------------------------------------------- verifica

def verify(chaos: dict, usage_txt: str, moveset_txt: str, n: int = 5) -> int:
    """Confronta i calcoli sul file chaos con i file di testo di Smogon.

    Se qualcosa non torna (per esempio Smogon cambia il formato dei file)
    meglio fermarsi che pubblicare numeri sbagliati.
    """
    data = chaos.get("data") or {}
    table = parse_usage_table(usage_txt)
    blocks = parse_moveset(moveset_txt)
    top_names = sorted(data, key=lambda k: data[k].get("usage") or 0, reverse=True)[:n]
    compared = 0

    for name in top_names:
        ours = float(data[name].get("usage") or 0) * 100
        if name not in table:
            raise BuildError(f"Verifica: {name} non compare nella tabella usage di Smogon")
        if abs(ours - table[name]) > 0.01:
            raise BuildError(f"Verifica usage fallita per {name}: "
                             f"calcolato {ours:.3f}%, Smogon {table[name]:.3f}%")
        compared += 1

        block = blocks.get(name)
        if not block:
            raise BuildError(f"Verifica: {name} non trovato nel file moveset di Smogon")
        total = section_total(data[name])
        for sec in CHECKED_SECTIONS:
            ours_sec = {to_id(k): v / total * 100 for k, v in (data[name].get(sec) or {}).items() if k}
            for label, theirs in block.get(sec, {}).items():
                if label == "Other":
                    continue
                k = to_id(label)
                if k not in ours_sec:
                    raise BuildError(f"Verifica: '{label}' ({sec}, {name}) manca nel file chaos")
                if abs(ours_sec[k] - theirs) > 0.05:
                    raise BuildError(f"Verifica fallita per {name} / {sec} / {label}: "
                                     f"calcolato {ours_sec[k]:.3f}%, Smogon {theirs:.3f}%")
                compared += 1

    if compared < n * 5:
        raise BuildError(f"Verifica: confrontati solo {compared} valori, troppo pochi")
    return compared


# --------------------------------------------------------------------------- scrittura

def history(out: Path, key: str) -> list[str]:
    folder = out / "data" / key
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}", p.stem))


def write_json(path: Path, obj: dict, compact: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def dataset_conf(config: dict, ds: dict) -> dict:
    defaults = config.get("defaults", {})
    conf = {**defaults, **ds}
    conf["top"] = {**defaults.get("top", {}), **ds.get("top", {})}
    conf.setdefault("min_battles", config.get("min_battles", 0))
    return conf


# --------------------------------------------------------------------------- main

def run(args: argparse.Namespace) -> int:
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    out = Path(args.out)
    base = args.base_url.rstrip("/") + "/"
    latest_path = out / "latest.json"
    previous = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.exists() else None

    enabled = {k: v for k, v in config["datasets"].items() if v.get("enabled", True)}
    if not enabled:
        raise BuildError("Nessun dataset attivo in config.json")

    mapping_path = Path(args.mapping)
    mapping = None
    if mapping_path.exists():
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        log(f"Collegamento al database dell'app: {mapping_path.name} del {mapping.get('generated_at', '?')}")
    else:
        log(f"{mapping_path.name} non trovato: i dati non avranno i game_id")
    mapping_version = mapping.get("generated_at") if mapping else None

    months = list_months(base)
    month = args.month or months[-1]
    if month not in months:
        raise BuildError(f"Il mese {month} non esiste su Smogon (ultimo disponibile: {months[-1]})")
    log(f"Mese: {month}")

    if (previous and not args.force and not args.month
            and previous.get("month") == month
            and set(previous.get("datasets", {})) == set(enabled)
            and previous.get("mapping") == mapping_version):
        log(f"I dati di {month} sono già pubblicati: niente da fare.")
        return 0

    formats = list_formats(base, month)

    # prima elaboro tutto, poi scrivo: o si aggiorna tutto o niente
    results: dict[str, tuple[dict, dict]] = {}
    for key, ds in enabled.items():
        log(f"\n[{key}]")
        conf = dataset_conf(config, ds)
        info = choose_format(base, month, formats, key, conf)
        stem = f"{info['format']}-{info['rating']}"
        log(f"  scelto: {stem}")
        chaos = fetch_json(f"{base}{month}/chaos/{stem}.json")
        if args.skip_check:
            log("  verifica saltata (--skip-check)")
        else:
            moveset_txt = fetch_text(f"{base}{month}/moveset/{stem}.txt")
            n = verify(chaos, info["usage_txt"], moveset_txt)
            log(f"  verifica superata: {n} valori uguali ai file di testo di Smogon")
        linker = Linker(mapping)
        dataset = build_dataset(chaos, conf, info, month, linker)
        log(f"  {len(dataset['pokemon'])} Pokémon, regolamento {dataset['regulation'] or '?'}")
        unmapped = linker.report()
        for sec, values in unmapped.items():
            log(f"  ATTENZIONE, non collegati ({sec}): {', '.join(values)}")
        results[key] = (unmapped, dataset)

    for key, (_, dataset) in results.items():
        write_json(out / "data" / key / f"{month}.json", dataset, compact=True)

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    if previous and month < previous.get("month", ""):
        # recupero di un mese vecchio: latest.json continua a puntare al più recente
        for key, entry in previous.get("datasets", {}).items():
            entry["history"] = history(out, key)
        previous["generated_at"] = now
        write_json(latest_path, previous, compact=False)
        log(f"\nSalvato {month} nello storico; latest.json resta su {previous['month']}.")
        return 0

    latest = {
        "schema": SCHEMA_VERSION,
        "month": month,
        "generated_at": now,
        "mapping": mapping_version,
        "source": SOURCE,
        "datasets": {},
    }
    for key, (unmapped, dataset) in results.items():
        entry = {
            "format": dataset["format"],
            "regulation": dataset["regulation"],
            "rating": dataset["rating"],
            "battles": dataset["battles"],
            "pokemon_count": len(dataset["pokemon"]),
            "url": f"data/{key}/{month}.json",
            "history": history(out, key),
        }
        if unmapped:
            entry["unmapped"] = unmapped
        latest["datasets"][key] = entry
    write_json(latest_path, latest, compact=False)
    log(f"\nPubblicati i dati di {month}: {', '.join(results)}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aggiorna gli usage di Pokémon Champions da Smogon")
    p.add_argument("--config", default=str(ROOT / "config.json"))
    p.add_argument("--out", default=str(ROOT / "docs"), help="cartella pubblicata (default: docs)")
    p.add_argument("--mapping", default=str(ROOT / "mapping.json"),
                   help="collegamento agli ID del database dell'app (creato da build_mapping.py)")
    p.add_argument("--month", help="mese da elaborare, es. 2026-09 (default: il più recente)")
    p.add_argument("--force", action="store_true", help="rigenera anche se il mese è già pubblicato")
    p.add_argument("--skip-check", action="store_true", help="salta il confronto con i file di testo")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except BuildError as e:
        log(f"\nERRORE: {e}")
        log("Nessun file è stato modificato.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
