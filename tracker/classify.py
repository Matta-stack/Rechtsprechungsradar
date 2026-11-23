"""Filtern nach Rechtsgebiet und KI-Kurzeinordnung.

Stufe 1: Schlagwortfilter (kostenlos) – sortiert offensichtlich Irrelevantes aus.
Stufe 2: Sprachmodell – prüft Relevanz, schreibt Kurzfassung und Kernaussagen.
Ohne API-Schlüssel läuft nur Stufe 1; dann wird der amtliche Leitsatz angezeigt.
"""
from __future__ import annotations

import json
import os
import re

PROMPT = """Du bist wissenschaftlicher Mitarbeiter in einer auf {gebiete} spezialisierten Kanzlei.
Prüfe die folgende Gerichtsentscheidung. Antworte AUSSCHLIESSLICH mit JSON in diesem Format:

{{
  "gebiete": [ ... Teilmenge von {keys}, leer wenn die Entscheidung für keines dieser Gebiete substanziell ist ... ],
  "titel": "prägnante Überschrift für Fachleser, max. 12 Wörter, ohne Aktenzeichen",
  "sachverhalt": "4-6 Sätze: Wer stritt mit wem worüber, was war konkret passiert, was wurde beantragt, wie entschieden die Vorinstanzen. Konkret und anschaulich, aber ohne Namen von Privatpersonen.",
  "entscheidung": "2-4 Sätze: Wie hat das Gericht entschieden und mit welcher tragenden Begründung?",
  "kurzfassung": "genau 2 Sätze für einen Newsletter: worum ging es und was ist das Ergebnis",
  "kernaussagen": ["höchstens 3 Rechtssätze, jeweils ein Satz"],
  "normen": ["zentrale Normen, z. B. Art. 82 Abs. 1 DSGVO"],
  "bedeutung": "hoch | mittel | gering",
  "praxis": "1 Satz: Was bedeutet das für Unternehmen/Berater?"
}}

Regeln:
- Stütze dich nur auf den mitgelieferten Text. Erfinde keine Aktenzeichen, Daten oder Zitate.
- Ein Gebiet nur angeben, wenn es für die Entscheidung tragend ist, nicht bei bloßer Erwähnung.
- Ist der Text unvollständig, sag das in der Kurzfassung.

Gericht: {gericht}
Datum: {datum}
Aktenzeichen: {aktenzeichen}
Typ: {typ}
Normen: {normen}
Leitsatz: {leitsatz}
Tenor: {tenor}

Text (gekürzt):
{text}
"""


def keyword_areas(doc: dict, areas: dict) -> list[str]:
    hay = " ".join(
        doc.get(k, "") for k in ("normen", "leitsatz", "titel", "tenor", "volltext")
    )
    hits = []
    for key, area in areas.items():
        if any(kw.lower() in hay.lower() for kw in area["keywords"]):
            hits.append(key)
    return hits


TOOL = {
    "name": "einordnung",
    "description": "Einordnung einer Gerichtsentscheidung für einen Fach-Newsletter.",
    "input_schema": {
        "type": "object",
        "properties": {
            "gebiete": {"type": "array", "items": {"type": "string"}},
            "titel": {"type": "string"},
            "sachverhalt": {"type": "string"},
            "entscheidung": {"type": "string"},
            "kurzfassung": {"type": "string"},
            "kernaussagen": {"type": "array", "items": {"type": "string"}},
            "normen": {"type": "array", "items": {"type": "string"}},
            "bedeutung": {"type": "string", "enum": ["hoch", "mittel", "gering"]},
            "praxis": {"type": "string"},
        },
        "required": ["gebiete", "titel", "sachverhalt", "entscheidung", "kurzfassung", "bedeutung"],
    },
}


class KIFehler(Exception):
    """Die KI-Einordnung ist fehlgeschlagen; der Eintrag wird beim nächsten Lauf erneut versucht."""


def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def llm_classify(doc: dict, areas: dict, llm_cfg: dict) -> dict:
    """Gibt die Einordnung zurück oder wirft KIFehler mit lesbarem Grund."""
    if not llm_available():
        raise KIFehler("kein ANTHROPIC_API_KEY gesetzt")
    import anthropic

    client = anthropic.Anthropic()
    prompt = PROMPT.format(
        gebiete=", ".join(a["label"] for a in areas.values()),
        keys=json.dumps(list(areas.keys())),
        text=doc.get("volltext", "")[: llm_cfg.get("max_chars", 24000)],
        **{k: doc.get(k, "") for k in ("gericht", "datum", "aktenzeichen", "typ", "normen", "leitsatz", "tenor")},
    ).replace("Antworte AUSSCHLIESSLICH mit JSON in diesem Format:", "Rufe das Werkzeug \"einordnung\" mit diesen Feldern auf:")
    try:
        msg = client.messages.create(
            model=llm_cfg["model"],
            max_tokens=3000,
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "einordnung"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        raise KIFehler(f"API-Fehler: {type(exc).__name__}: {str(exc)[:300]}") from exc
    data = next((b.input for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
    if not isinstance(data, dict):
        raise KIFehler(f"keine strukturierte Antwort (stop_reason={msg.stop_reason})")
    data["gebiete"] = [g for g in data.get("gebiete", []) if g in areas]
    return data


def selbsttest(areas: dict, llm_cfg: dict) -> None:
    """Kurzer Test, ob API-Schlüssel und Modell funktionieren."""
    probe = {
        "gericht": "BGH", "datum": "2026-01-01", "aktenzeichen": "TEST", "typ": "Urteil",
        "normen": "Art. 82 DSGVO", "leitsatz": "Kontrollverlust über Daten kann ein immaterieller Schaden sein.",
        "tenor": "", "volltext": "Die Beklagte veröffentlichte versehentlich Kundendaten. Der Kläger verlangt Schadensersatz nach Art. 82 DSGVO.",
    }
    try:
        r = llm_classify(probe, areas, llm_cfg)
        print(f"KI-Selbsttest: OK (Modell {llm_cfg['model']}, Gebiete {r['gebiete']})")
    except KIFehler as exc:
        print(f"KI-Selbsttest: FEHLER – {exc}")
        raise SystemExit(1)
