"""Täglicher Lauf: abholen -> filtern -> einordnen -> speichern -> Website bauen.

Aufruf:  python -m tracker.run            (normaler Lauf)
         python -m tracker.run --build    (nur Website aus vorhandenen Daten bauen)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

from . import classify, sources

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "decisions.json"
SITE_SRC = ROOT / "site"
OUT = ROOT / "public"


def doc_id(d: dict) -> str:
    return d.get("ecli") or d.get("celex") or f"{d['gericht']}|{d['aktenzeichen']}|{d['datum']}"


def load() -> list[dict]:
    return json.loads(DATA.read_text("utf-8")) if DATA.exists() else []


def save(items: list[dict]) -> None:
    items.sort(key=lambda d: d["datum"], reverse=True)
    DATA.write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")


def collect(cfg: dict) -> list[dict]:
    days = int(os.environ.get("LOOKBACK_DAYS") or cfg["lookback_days"])
    since = dt.date.today() - dt.timedelta(days=days)
    print(f"Zeitraum: seit {since.isoformat()} ({days} Tage)")
    print("KI-Einordnung:", "aktiv" if os.environ.get("ANTHROPIC_API_KEY") else "aus (kein ANTHROPIC_API_KEY)")
    found: list[dict] = []

    def attempt(name: str, fn) -> list[dict] | None:
        # Fällt eine Quelle aus, läuft der Rest trotzdem weiter.
        print(f"{name} …")
        try:
            res = fn()
            print(f"  {len(res)} Entscheidungen")
            return res
        except Exception as exc:
            print(f"  ! {name} nicht erreichbar: {type(exc).__name__}: {exc}")
            return None

    # Bundesgerichte: zuerst NeuRIS, sonst rechtsprechung-im-internet.de
    bund = None
    if cfg["sources"].get("neuris", {}).get("enabled"):  # derzeit abgeschaltet (Adresse liefert 410)
        bund = attempt("Bundesgerichte (rechtsinformationen.bund.de)", lambda: sources.fetch_neuris(since))
    if bund is None and cfg["sources"]["rii"]["enabled"]:
        bund = attempt("Bundesgerichte (rechtsprechung-im-internet.de)",
                       lambda: sources.fetch_rii(cfg["sources"]["rii"]["toc_url"], since))
    found += bund or []

    if cfg["sources"]["eu"]["enabled"]:
        found += attempt("EuGH (EUR-Lex)", lambda: sources.fetch_eu(
            cfg["sources"]["eu"]["sparql_url"], cfg["sources"]["eu"]["text_url"], since)) or []
    print(f"Insgesamt {len(found)} Entscheidungen im Zeitraum")
    return found


def process(cfg: dict, items: list[dict], found: list[dict]) -> int:
    # Einträge ohne KI-Prüfung (aus früheren Läufen) entfernen; sie werden neu bewertet
    vorher = len(items)
    items[:] = [i for i in items if i.get("redaktionell")
                or (i.get("ki_geprueft") and i.get("ki_version", 1) >= classify.KI_VERSION)]
    if vorher != len(items):
        print(f"{vorher - len(items)} Einträge (ungeprüft oder ältere KI-Version) entfernt, sie werden neu bewertet")
    known = {d.get("id") or doc_id(d) for d in items}
    added = 0
    fehler = 0
    print("Prüfung der gefundenen Entscheidungen:")
    for d in found:
        kw = classify.keyword_areas(d, cfg["areas"])
        print(f"  · {d.get('gericht','')[:30]:30} {d.get('aktenzeichen','')[:22]:22} "
              f"{d.get('datum','')}  Text: {len(d.get('volltext','')):>6} Zeichen  "
              f"Treffer: {', '.join(kw) or '–'}")
        if doc_id(d) in known or added >= cfg["max_new_per_run"]:
            continue
        if not kw:
            continue
        if not classify.llm_available():
            continue  # ohne KI keine Veröffentlichung, nur Schlagwort-Treffer sind zu ungenau
        try:
            ai = classify.llm_classify(d, cfg["areas"], cfg["llm"])
        except classify.KIFehler as exc:
            print(f"  ! KI-Einordnung fehlgeschlagen ({d.get('aktenzeichen')}): {exc} – neuer Versuch beim nächsten Lauf")
            fehler += 1
            if fehler >= 5:
                print("  ! 5 KI-Fehler in Folge – Lauf wird beendet, bitte Protokoll prüfen")
                break
            continue
        fehler = 0
        gebiete = ai["gebiete"]
        if not gebiete:
            print(f"    KI: nicht einschlägig – {d.get('aktenzeichen')}")
            continue
        entry = {k: d.get(k, "") for k in ("gericht", "datum", "aktenzeichen", "ecli", "typ", "url", "quelle", "leitsatz")}
        entry.update(
            {
                "id": doc_id(d),
                "gebiete": gebiete,
                "titel": (ai.get("titel") or d.get("titel", "").replace("#", " – "))[:300],
                "sachverhalt": (ai or {}).get("sachverhalt", ""),
                "entscheidung": (ai or {}).get("entscheidung", ""),
                "kurzfassung": (ai or {}).get("kurzfassung", ""),
                "kernaussagen": (ai or {}).get("kernaussagen", []),
                "normen": (ai or {}).get("normen", []) or [n.strip() for n in d.get("normen", "").split(",") if n.strip()][:6],
                "bedeutung": (ai or {}).get("bedeutung", "mittel"),
                "praxis": (ai or {}).get("praxis", ""),
                "ki_geprueft": True,
                "ki_version": classify.KI_VERSION,
                "einschlaegigkeit": ai.get("einschlaegigkeit", {}),
                "merkmale": ai.get("merkmale", {}),
                "erfasst": dt.date.today().isoformat(),
            }
        )
        items.append(entry)
        known.add(entry["id"])
        added += 1
        print(f"  + {entry['gericht']} {entry['aktenzeichen']} → {', '.join(gebiete)}")
    return added


def build(cfg: dict, items: list[dict]) -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(SITE_SRC, OUT)
    payload = {
        "site": cfg["site"],
        "areas": {k: v["label"] for k, v in cfg["areas"].items()},
        "stand": dt.datetime.now().strftime("%d.%m.%Y, %H:%M"),
        "items": items,
    }
    (OUT / "data.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    for key, label in payload["areas"].items():
        (OUT / f"feed-{key}.xml").write_text(rss(cfg, key, label, items), "utf-8")
    print(f"Website gebaut: {OUT}")


def rss(cfg: dict, key: str, label: str, items: list[dict]) -> str:
    base = cfg["site"].get("base_url", "").rstrip("/")
    entries = []
    for d in [i for i in items if key in i["gebiete"]][:50]:
        when = dt.datetime.fromisoformat(d["datum"]).replace(tzinfo=dt.timezone.utc)
        entries.append(
            f"<item><title>{escape(d['gericht'] + ', ' + d['aktenzeichen'])}</title>"
            f"<link>{escape(d['url'])}</link><guid isPermaLink=\"false\">{escape(d['id'])}</guid>"
            f"<pubDate>{format_datetime(when)}</pubDate>"
            f"<description>{escape(d.get('kurzfassung') or d.get('leitsatz') or '')}</description></item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{escape(cfg['site']['title'])} – {escape(label)}</title>"
        f"<link>{escape(base or 'https://example.org')}</link>"
        f"<description>Neue Entscheidungen: {escape(label)}</description>"
        + "".join(entries)
        + "</channel></rss>"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="nur Website bauen")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    items = load()
    if not args.build:
        if classify.llm_available():
            classify.selbsttest(cfg["areas"], cfg["llm"])
        added = process(cfg, items, collect(cfg))
        print(f"{added} neue Einträge")
        save(items)
    build(cfg, items)


if __name__ == "__main__":
    main()
