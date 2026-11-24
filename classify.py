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
Prüfe die folgende Gerichtsentscheidung. Rufe das Werkzeug "einordnung" mit diesen Feldern auf:

{{
  "einschlaegigkeit": {{ Punktwert 0-3 für jeden dieser Schlüssel: {keys} }},
  "begruendung_gebiete": "1 Satz: warum diese Punktwerte",
  "titel": "prägnante Überschrift für Fachleser, max. 12 Wörter, ohne Aktenzeichen",
  "sachverhalt": "4-6 Sätze: Wer stritt mit wem worüber, was war konkret passiert, was wurde beantragt, wie entschieden die Vorinstanzen. Konkret und anschaulich, aber ohne Namen von Privatpersonen.",
  "entscheidung": "2-4 Sätze: Wie hat das Gericht entschieden und mit welcher tragenden Begründung?",
  "kurzfassung": "genau 2 Sätze für einen Newsletter: worum ging es und was ist das Ergebnis",
  "kernaussagen": ["höchstens 3 Rechtssätze, jeweils ein Satz"],
  "normen": ["zentrale Normen, z. B. Art. 82 Abs. 1 DSGVO"],
  "bedeutung": "hoch | mittel | gering",
  "praxis": "1 Satz: Was bedeutet das für Unternehmen/Berater?",
  "merkmale": {{ "Gebiets-Schlüssel": ["zutreffende Merkmale, nur aus der Liste des Gebiets"] }}
}}

Definition der Rechtsgebiete:
{definitionen}

Merkmale je Rechtsgebiet (für den Abgleich mit Mandatsakten):
{merkmalliste}

Regeln:
- Stütze dich nur auf den mitgelieferten Text. Erfinde keine Aktenzeichen, Daten oder Zitate.
- Bewerte für JEDES Gebiet die Einschlägigkeit von 0 bis 3:
  0 = gar nicht, 1 = nur am Rande erwähnt, 2 = ein tragender Teil der Entscheidung, 3 = Kernthema.
  Sei streng. Im Zweifel 0 oder 1. Die meisten EuGH-Urteile betreffen keines der Gebiete.
- Bei Urteilen steht das Ergebnis (Tenor) meist am Ende des Textes; nutze es für "entscheidung".
- Fehlt das Ergebnis tatsächlich, schreibe das knapp, statt zu spekulieren.
- "merkmale": nur für Gebiete mit Punktwert 2 oder 3; wähle 1 bis 3 Merkmale wörtlich aus der Liste des Gebiets.

Gericht: {gericht}
Datum: {datum}
Aktenzeichen: {aktenzeichen}
Typ: {typ}
Normen: {normen}
Leitsatz: {leitsatz}
Tenor: {tenor}

Text (bei langen Entscheidungen: Anfang und Ende, Mitte gekürzt):
{text}
"""


def _court_ok(doc: dict, area: dict) -> bool:
    courts = area.get("gerichte")
    if not courts:
        return True
    g = (doc.get("gericht") or "").strip()
    return any(g == c or g.startswith(c + " ") for c in courts)


def keyword_areas(doc: dict, areas: dict) -> list[str]:
    """Vorfilter: Gericht passt (falls angegeben) und ein Schlagwort kommt vor.
    Gebiete ohne Schlagworte (z. B. Arbeitsrecht beim BAG) nehmen jede Entscheidung des Gerichts."""
    hay = " ".join(
        doc.get(k, "") for k in ("normen", "leitsatz", "titel", "tenor", "volltext")
    ).lower()
    hits = []
    g = (doc.get("gericht") or "").strip()
    for key, area in areas.items():
        if _court_ok(doc, area):
            kws = area.get("keywords") or []
            if not kws or any(kw.lower() in hay for kw in kws):
                hits.append(key)
            continue
        for court, kws in (area.get("weitere_gerichte") or {}).items():
            if (g == court or g.startswith(court + " ")) and any(kw.lower() in hay for kw in kws):
                hits.append(key)
                break
    return hits


TOOL = {
    "name": "einordnung",
    "description": "Einordnung einer Gerichtsentscheidung für einen Fach-Newsletter.",
    "input_schema": {
        "type": "object",
        "properties": {
            "einschlaegigkeit": {
                "type": "object",
                "description": "Punktwert 0-3 je Gebiets-Schlüssel",
                "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 3},
            },
            "begruendung_gebiete": {"type": "string", "description": "1 Satz, warum diese Punktwerte"},
            "titel": {"type": "string"},
            "sachverhalt": {"type": "string"},
            "entscheidung": {"type": "string"},
            "kurzfassung": {"type": "string"},
            "kernaussagen": {"type": "array", "items": {"type": "string"}},
            "normen": {"type": "array", "items": {"type": "string"}},
            "bedeutung": {"type": "string", "enum": ["hoch", "mittel", "gering"]},
            "praxis": {"type": "string"},
            "merkmale": {
                "type": "object",
                "description": "je Gebiets-Schlüssel eine Liste von Merkmalen aus der vorgegebenen Liste",
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
            },
        },
        "required": ["einschlaegigkeit", "begruendung_gebiete", "titel", "sachverhalt", "entscheidung", "kurzfassung", "bedeutung"],
    },
}


KI_VERSION = 2  # erhöhen, wenn sich Prompt/Regeln ändern -> ältere Einträge werden neu bewertet


def _kopf_und_ende(text: str, max_chars: int) -> str:
    """Anfang (Sachverhalt, Vorlagefragen) und Ende (Würdigung, Tenor) behalten."""
    if len(text) <= max_chars:
        return text
    kopf = max_chars // 3
    return text[:kopf] + "\n[…Mittelteil gekürzt…]\n" + text[-(max_chars - kopf):]


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
        text=_kopf_und_ende(doc.get("volltext", ""), llm_cfg.get("max_chars", 24000)),
        definitionen="\n".join(f"- {k} ({a['label']}): {a.get('beschreibung', '')}" for k, a in areas.items()),
        merkmalliste="\n".join(f"- {k}: {', '.join(a['merkmale'])}" for k, a in areas.items() if a.get("merkmale")) or "-",
        **{k: doc.get(k, "") for k in ("gericht", "datum", "aktenzeichen", "typ", "normen", "leitsatz", "tenor")},
    )
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
    punkte = data.get("einschlaegigkeit") or {}
    data["gebiete"] = [k for k in areas if int(punkte.get(k, 0) or 0) >= 2]
    roh = data.get("merkmale") or {}
    data["merkmale"] = {
        k: [m for m in (roh.get(k) or []) if m in areas[k].get("merkmale", [])][:3]
        for k in data["gebiete"] if areas[k].get("merkmale")
    }
    if not data.get("kurzfassung"):
        data["kurzfassung"] = (data.get("sachverhalt") or "").split(". ")[0][:400]
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
