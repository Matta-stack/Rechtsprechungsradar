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


def llm_classify(doc: dict, areas: dict, llm_cfg: dict) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    import anthropic  # nur nötig, wenn ein Schlüssel gesetzt ist

    client = anthropic.Anthropic(api_key=api_key)
    prompt = PROMPT.format(
        gebiete=", ".join(a["label"] for a in areas.values()),
        keys=json.dumps(list(areas.keys())),
        text=doc.get("volltext", "")[: llm_cfg.get("max_chars", 24000)],
        **{k: doc.get(k, "") for k in ("gericht", "datum", "aktenzeichen", "typ", "normen", "leitsatz", "tenor")},
    )
    msg = client.messages.create(
        model=llm_cfg["model"],
        max_tokens=1600,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(getattr(b, "text", "") for b in msg.content)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    data["gebiete"] = [g for g in data.get("gebiete", []) if g in areas]
    return data
