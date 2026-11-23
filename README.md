# Rechtsprechungs-Radar Datenschutz & KI

Holt täglich neue Entscheidungen der Bundesgerichte (rechtsprechung-im-internet.de) und des EuGH (EUR-Lex/CELLAR),
filtert sie nach Datenschutz- und KI-Recht, lässt sie per KI kurz einordnen und veröffentlicht eine Website mit RSS-Feeds.

## Einrichtung (ca. 30 Minuten, einmalig)

1. **GitHub-Konto anlegen** auf github.com (kostenlos).
2. **Neues Repository** anlegen, z. B. `rechtsprechungs-radar`, Sichtbarkeit „Public“ (GitHub Pages ist für öffentliche Repos kostenlos).
3. **Dateien hochladen**: Inhalt dieses Ordners per „Add file → Upload files“ hochladen (inkl. des Ordners `.github`).
4. **API-Schlüssel hinterlegen**: Unter console.anthropic.com einen API-Schlüssel erzeugen, ein kleines Ausgabenlimit setzen
   (z. B. 10 € / Monat). Im Repository: Settings → Secrets and variables → Actions → „New repository secret“,
   Name `ANTHROPIC_API_KEY`, Wert = Schlüssel. Ohne Schlüssel läuft der Tracker auch, zeigt dann aber nur amtliche Leitsätze.
5. **Website einschalten**: Settings → Pages → Source: „GitHub Actions“.
6. **Erster Lauf**: Reiter „Actions“ → „Rechtsprechung aktualisieren“ → „Run workflow“. Danach läuft er täglich um ca. 6:17 Uhr.
7. In `config.yaml` unter `site.base_url` die Adresse eintragen, die GitHub unter Settings → Pages anzeigt.

## Vor dem öffentlichen Start

- Impressum (§ 5 DDG) und Datenschutzerklärung auf der Seite ergänzen.
- Die vorhandenen Einträge in `data/decisions.json` sind redaktionell gepflegte Startdaten; Einträge mit
  `"redaktionell": true` vor Veröffentlichung gegen den Originaltext prüfen.
- Ersten automatischen Lauf kontrollieren: Die Anbindung an rechtsprechung-im-internet.de und die EuGH-Abfrage (SPARQL)
  konnte noch nicht live getestet werden. Fehler stehen im Protokoll unter „Actions“.

## Anpassen

- Rechtsgebiete und Suchbegriffe: `config.yaml` → `areas`
- Zeitraum und Kostenbremse: `lookback_days`, `max_new_per_run`
- Aussehen: `site/index.html`

## Lokal testen

```
pip install -r requirements.txt
python -m tracker.run --build     # nur Website aus vorhandenen Daten bauen → Ordner public/
python -m tracker.run             # voller Lauf (Internetzugang nötig)
```

## Geplante Ausbaustufen

- Oberlandesgerichte und Landgerichte über Open Legal Data und Landesportale
- Pressemitteilungen von BGH, BAG und EuGH als Frühwarnung
- Verknüpfung „EuGH entscheidet → BGH setzt um“ über Zitate
- Wöchentlicher E-Mail-Newsletter
