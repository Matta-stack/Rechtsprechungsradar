"""Abholen neuer Entscheidungen aus offiziellen, frei zugänglichen Quellen.

- Bundesgerichte + BVerfG: rechtsprechung-im-internet.de (XML-Inhaltsverzeichnis + ZIP je Entscheidung)
- EuGH (Urteile und Schlussanträge): CELLAR-SPARQL des Amts für Veröffentlichungen + Volltext über EUR-Lex

Alle Texte sind amtliche Werke (§ 5 UrhG) bzw. frei weiterverwendbare EU-Dokumente.
"""
from __future__ import annotations

import datetime as dt
import io
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from html import unescape

import requests

UA = {"User-Agent": "Rechtsprechungs-Radar/0.1 (nicht-kommerzielles Monitoring; Kontakt siehe Impressum)"}
TIMEOUT = (15, 60)  # (Verbindungsaufbau, Lesen) in Sekunden


def _get(url: str, tries: int = 2, **kw) -> requests.Response:
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


# ------------------------------------------- Bundesgerichte über NeuRIS (neu)

NEURIS_HOSTS = ["https://rechtsinformationen.bund.de", "https://testphase.rechtsinformationen.bund.de"]
NEURIS = NEURIS_HOSTS[0]


def fetch_neuris(since: dt.date, max_pages: int = 10) -> list[dict]:
    """Neue Entscheidungen der Bundesgerichte über das Rechtsinformationsportal des Bundes."""
    global NEURIS
    last_exc: Exception | None = None
    for host in NEURIS_HOSTS:
        try:
            _get(f"{host}/v1/case-law", tries=1, params={"size": 1})
            NEURIS = host
            break
        except Exception as exc:
            last_exc = exc
    else:
        raise RuntimeError(f"keine NeuRIS-Adresse erreichbar ({last_exc})")
    out, seen = [], set()
    for page in range(max_pages):
        r = _get(
            f"{NEURIS}/v1/case-law",
            params={"dateFrom": since.isoformat(), "size": 100, "pageIndex": page},
        )
        members = r.json().get("member", [])
        if not members:
            break
        for m in members:
            it = m.get("item", {})
            key = it.get("documentNumber") or it.get("@id")
            if not key or key in seen:
                continue
            seen.add(key)
            snippets = " ".join(t.get("text", "") for t in m.get("textMatches", []) if t.get("text"))
            volltext = snippets
            try:  # Volltext als HTML, falls angeboten
                html = _get(f"{NEURIS}{it['@id']}.html", tries=1).text
                volltext = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip() or snippets
            except Exception:
                pass
            gericht = " ".join(x for x in (it.get("courtName") or it.get("courtType", ""), it.get("judicialBody", "")) if x)
            out.append(
                {
                    "quelle": "rechtsinformationen.bund.de",
                    "gericht": gericht,
                    "datum": (it.get("decisionDate") or "")[:10],
                    "aktenzeichen": ", ".join(it.get("fileNumbers") or []),
                    "ecli": it.get("ecli", ""),
                    "typ": it.get("documentType") or "Entscheidung",
                    "titel": it.get("headline", ""),
                    "normen": "",
                    "leitsatz": "",
                    "tenor": "",
                    "volltext": volltext,
                    "url": f"{NEURIS}/case-law/{it.get('documentNumber', '')}",
                }
            )
            time.sleep(0.3)
        if len(members) < 100:
            break
    return [d for d in out if d["datum"] and d["datum"] >= since.isoformat()]


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    raw = ET.tostring(el, encoding="unicode", method="text")
    return re.sub(r"\s+", " ", unescape(raw)).strip()


# ---------------------------------------------------------------- Bundesgerichte

def _loose_date(raw: str) -> dt.date | None:
    digits = re.sub(r"\D", "", raw or "")[:8]
    try:
        return dt.datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


def fetch_rii(toc_url: str, since: dt.date, max_docs: int = 800) -> list[dict]:
    """Seit `since` neu veröffentlichte Entscheidungen der Bundesgerichte.

    Maßgeblich ist das Veröffentlichungs-/Änderungsdatum (<modified>), nicht das
    Entscheidungsdatum: Urteile erscheinen oft erst Wochen nach der Verkündung.
    """
    toc = ET.fromstring(_get(toc_url).content)
    # Alte Entscheidungen, die nur nachträglich geändert wurden, interessieren nicht:
    # nur Entscheidungen der letzten 18 Monate berücksichtigen.
    oldest = since - dt.timedelta(days=540)
    kandidaten = []
    for item in toc.iter("item"):
        datum = _loose_date(item.findtext("entsch-datum") or "")
        if not datum or datum < oldest:
            continue
        geaendert = _loose_date(item.findtext("modified") or "") or datum
        if max(datum, geaendert) < since:
            continue
        kandidaten.append((max(datum, geaendert), datum, item))
    # Neueste zuerst, damit bei Erreichen der Obergrenze nichts Aktuelles fehlt
    kandidaten.sort(key=lambda k: k[0], reverse=True)
    print(f"  rii: {len(kandidaten)} Kandidaten im Zeitraum")
    if len(kandidaten) > max_docs:
        print(f"  rii: Obergrenze {max_docs} – ältere Kandidaten werden beim nächsten Lauf geholt")
    out = []
    for _, datum, item in kandidaten[:max_docs]:
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        try:
            doc = _parse_rii_zip(_get(link).content)
        except Exception as exc:  # einzelne defekte Dokumente überspringen
            print(f"  ! rii: {link}: {exc}")
            continue
        doc.update(
            {
                "quelle": "rechtsprechung-im-internet.de",
                "gericht": (item.findtext("gericht") or doc.get("gericht", "")).strip(),
                "datum": datum.isoformat(),
                "aktenzeichen": (item.findtext("aktenzeichen") or doc.get("aktenzeichen", "")).strip(),
                "url": _rii_public_url(doc.get("doknr", ""), link),
            }
        )
        out.append(doc)
        time.sleep(0.5)  # Server schonen
    return out


def _parse_rii_zip(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(z.read(name))
    g = lambda tag: _text(root.find(f".//{tag}"))
    gruende = " ".join(
        t for t in (g("gruende"), g("entscheidungsgruende"), g("tatbestand")) if t
    )
    if not gruende:  # manche Dokumente nutzen andere Felder: dann ganzes Dokument
        gruende = _text(root)
    return {
        "doknr": g("doknr"),
        "ecli": g("ecli"),
        "gericht": " ".join(t for t in (g("gertyp"), g("spruchkoerper")) if t),
        "aktenzeichen": g("aktenzeichen"),
        "typ": g("doktyp") or "Entscheidung",
        "normen": g("norm"),
        "titel": g("titelzeile"),
        "leitsatz": g("leitsatz"),
        "tenor": g("tenor"),
        "volltext": gruende,
    }


def _rii_public_url(doknr: str, fallback: str) -> str:
    if doknr:
        return f"https://www.rechtsprechung-im-internet.de/jportal/?quelle=jlink&docid={doknr}&psml=bsjrsprod.psml&max=true"
    return fallback


# ------------------------------------------------------------------------ EuGH

SPARQL = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT DISTINCT ?celex ?date ?ecli ?title WHERE {
  ?work cdm:resource_legal_id_celex ?celex ;
        cdm:work_date_document ?date .
  FILTER(REGEX(STR(?celex), "^6[0-9]{4}C[JC]"))
  FILTER(?date >= "%s"^^xsd:date)
  OPTIONAL { ?work cdm:case-law_ecli ?ecli . }
  OPTIONAL {
    ?expr cdm:expression_belongs_to_work ?work ;
          cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/DEU> ;
          cdm:expression_title ?title .
  }
}
ORDER BY DESC(?date)
LIMIT 300
"""


def fetch_eu(sparql_url: str, text_url: str, since: dt.date) -> list[dict]:
    """Urteile (CJ) und Schlussanträge (CC) des EuGH seit `since`."""
    r = requests.get(
        sparql_url,
        params={"query": SPARQL % since.isoformat(), "format": "application/sparql-results+json"},
        headers=UA,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    seen, out = set(), []
    for b in r.json()["results"]["bindings"]:
        celex = b["celex"]["value"]
        if "_" in celex:  # Zusammenfassungen/Leitsätze/Informationen: nur Hauptdokument
            continue
        if celex in seen:
            continue
        seen.add(celex)
        typ = "Urteil" if celex[5:7] == "CJ" else "Schlussanträge"
        volltext = _eu_text(celex, text_url)
        if not volltext:
            print(f"  ! eu: kein deutscher Volltext für {celex} (evtl. noch nicht übersetzt)")
        az = _case_number(celex)
        out.append(
            {
                "quelle": "EUR-Lex / CELLAR",
                "gericht": "EuGH" if typ == "Urteil" else "EuGH (Generalanwalt)",
                "datum": b["date"]["value"][:10],
                "aktenzeichen": az,
                "ecli": b.get("ecli", {}).get("value", ""),
                "typ": typ,
                "titel": b.get("title", {}).get("value", ""),
                "normen": "",
                "leitsatz": "",
                "tenor": "",
                "volltext": volltext,
                "url": f"https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX:{celex}",
                "celex": celex,
            }
        )
        time.sleep(0.5)
    return out


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _looks_like_judgment(text: str) -> bool:
    return len(text) > 3000 and any(w in text for w in ("Rechtssache", "URTEIL", "Urteil", "SCHLUSSANTRÄGE"))


def _eu_text(celex: str, text_url: str) -> str:
    """Deutscher Volltext: erst direkt aus CELLAR, dann über die EUR-Lex-Seite."""
    attempts = [
        (f"http://publications.europa.eu/resource/celex/{celex}",
         {"Accept": "text/html, application/xhtml+xml;q=0.9", "Accept-Language": "deu, de"}),
        (text_url.format(celex=celex), {}),
    ]
    for url, extra in attempts:
        try:
            r = requests.get(url, headers={**UA, **extra}, timeout=TIMEOUT, allow_redirects=True)
            if r.status_code != 200:
                continue
            text = _html_to_text(r.text)
            if _looks_like_judgment(text):
                return text
        except requests.RequestException:
            continue
    return ""


def _case_number(celex: str) -> str:
    # 62024CJ0526 -> C-526/24
    m = re.match(r"6(\d{4})C[JC](\d{4})", celex)
    if not m:
        return celex
    return f"C-{int(m.group(2))}/{m.group(1)[2:]}"
