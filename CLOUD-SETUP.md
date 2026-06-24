# Cloud Setup — Printeers Stock Monitoring (laptop-unabhaengig)

Ziel: Der Report laeuft taeglich 06:00 und montags 07:00 in der Cloud (GitHub
Actions), voellig unabhaengig davon, ob dein Laptop an ist.

## Was du einmalig machst

1. **Repo anlegen**
   - Auf https://github.com einen neuen, *privaten* Repo erstellen,
     z.B. `heydear-stock-monitoring`.

2. **Diese Dateien hochladen** (Inhalt dieses Ordners), und zwar:
   - `stock_report.py`
   - `snapshots/` (mit dem vorhandenen Snapshot als Startpunkt)
   - `.github/workflows/stock.yml`
   - `.gitignore`
   - **NICHT** den Ordner `config/` hochladen. Er enthaelt dein Geheimnis und
     ist per `.gitignore` ausgeschlossen. Die Secrets kommen stattdessen in
     GitHub Secrets (siehe naechster Schritt).

3. **Secrets hinterlegen**
   Repo → Settings → Secrets and variables → Actions → *New repository secret*.
   Lege an:
   - `PRINTEERS_SECRET_KEY`  = dein Printeers V2 Secret Key
   - `SLACK_WEBHOOK_URL`     = deine Slack Incoming Webhook URL
   - `PRINTEERS_ENV`         = `production`  (optional)
   - `SLACK_BOT_NAME`        = `HEY DEAR Stock Bot`  (optional)
   - `SLACK_BOT_ICON`        = `:package:`  (optional)

4. **Schreibrechte fuer den Workflow**
   Repo → Settings → Actions → General → Workflow permissions →
   *Read and write permissions* aktivieren (damit die taeglichen Snapshots
   zurueck ins Repo committet werden koennen).

5. **Testlauf**
   Repo → Actions → Workflow "Printeers Stock Monitoring" → *Run workflow*
   (workflow_dispatch). Danach sollte die Nachricht in #supply-chain erscheinen.

6. **Doppelposts vermeiden**
   Sobald die Cloud zuverlaessig laeuft, die beiden lokalen Cowork-Zeitplaene
   (`printeers-stock-report`, `printeers-weekly-balance`) deaktivieren oder
   loeschen, damit nicht doppelt gepostet wird.

## Zeitzonen-Hinweis (wichtig)

GitHub Cron laeuft in **UTC**. Eingestellt ist:
- `0 4 * * *`  = 06:00 Berlin im **Sommer** (CEST). Im **Winter** (CET) waeren
  das 05:00 Berlin → dann in `stock.yml` auf `0 5 * * *` aendern.
- `0 5 * * 1`  = 07:00 Berlin im Sommer (Montag). Im Winter `0 6 * * 1`.

GitHub passt Sommer/Winterzeit nicht automatisch an. Zweimal im Jahr kurz die
zwei Cron-Zeilen anpassen, dann passt die Uhrzeit wieder.

## Wie es funktioniert

- `stock_report.py` liest die Zugangsdaten zuerst aus Umgebungsvariablen
  (GitHub Secrets), sonst aus `config/printeers-config.txt` (lokal).
- Der taegliche Job committet den neuen Snapshot zurueck ins Repo, damit der
  Vergleich Tag-zu-Tag und die Wochenbilanz auch in der Cloud Historie haben.
- Posten erfolgt per Webhook direkt aus dem Skript, kein Slack-Connector noetig.
