# Mandate Bot

Scrapes active tenders from two government procurement portals, downloads
tender documents for anything tagged as a legal-adjacent procurement type,
scans the document text for legal-services keywords, and saves the
documents for any match into `downloads/`.

Sources:
- **Punjab (Pakistan) e-Procurement portal** (`eproc.punjab.gov.pk`)
- **Balochistan Public Procurement Regulatory Authority** (`bpptwo.vdc.services`)

## How it works

### Punjab (`mandate_bot/scraper.py`)
1. Loads `ActiveTenders.aspx` and walks every page of the results grid (it's
   an ASP.NET WebForms `RadGrid`, so paging is done by replaying its
   `__doPostBack` viewstate mechanism — see `aspnet_form.py`).
2. Rows are filtered down to the categories listed in `config.yaml`
   (`Services`, `Consultancy` by default — the portal has no dedicated
   "Legal" category, so this is a coarse pre-filter).
3. For each new tender, both linked PDFs ("Tender Notice" and "Bidding
   Document") are downloaded and their text is extracted with `pdfplumber`,
   falling back to OCR (Tesseract) page-by-page for any page with no
   embedded text layer (i.e. scanned/image-only pages) — see `pdf_utils.py`.

### Balochistan / BPPT (`mandate_bot/bppt.py`)
This portal is a **Blazor Server** app: its category filters and pagination
run over a live WebSocket connection, and its "documents" are Angular-
templated report pages, not real PDFs (a plain HTTP fetch returns
unrendered `{{...}}` placeholders). So this source is scraped with a
headless browser (Playwright) instead of plain HTTP requests:
1. Clicks the `Services` / `Consulting Services` category filter buttons
   and pages through the results grid.
2. For each new tender, renders both linked report pages ("Bidding
   Document" and "NIT Report") in the browser and reads their final text.
3. If a match is found, a PDF snapshot of each rendered page (`page.pdf()`)
   is saved as the archived document.

### Both sources
The combined text is checked against the keyword list in `config.yaml`. If
any keyword hits, the documents are saved into
`downloads/<publish-date>_<title>/` and a row is appended to
`logs/matches.csv` (with a `source` column). Every tender checked (match or
not) is recorded in `state/seen.json` so re-runs only process newly
published tenders.

## Setup

```bash
cd "Mandate Bot"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium   # one-time browser download (~300MB), needed for the BPPT source
```

Also needs, via Homebrew (for OCR on the Punjab source):
```bash
brew install tesseract poppler
```

## Run manually

```bash
./run.sh
# or: source .venv/bin/activate && python3 -m mandate_bot.main
```

A full run (both sources) currently takes on the order of tens of minutes,
since every candidate document is downloaded/rendered and read individually
with a polite delay between requests — this is normal, let it finish.

Output:
- Matched documents: `downloads/`
- Match log (CSV, one row per match, includes which source it came from): `logs/matches.csv`
- Full run log: `logs/run.log`
- Dedupe state: `state/seen.json`

### Forcing a re-scan

```bash
./run.sh  # normally
python3 -m mandate_bot.main --rescan-last 50   # force re-check the 50 most recently listed candidates, even if already marked seen
```

## Configuration (`config.yaml`)

- `source.insecure_skip_verify` — **the Punjab portal's TLS certificate is
  currently expired** (a problem on their server, not this bot). This flag
  disables certificate verification so the bot can reach the site at all.
  It's a read-only public listing (no login/credentials sent), so the risk
  is limited, but flip this off the day they fix their cert. (Balochistan's
  portal has a valid certificate, no such flag needed there.)
- `source.max_pages` / `bppt.max_pages` — safety cap on how many grid pages
  to walk per run, per source.
- `categories` (Punjab) / `bppt.categories` (Balochistan) — which listing
  categories to bother downloading/scanning. Balochistan's categories are a
  proper UNSPSC-style taxonomy shown per row; Punjab's is a crude
  Goods/Services/Works/Consultancy split.
- `keywords` — case-insensitive substring match against extracted document
  text, shared by both sources. Tuned to avoid boilerplate false positives:
  generic terms like "attorney", "litigation", "firm", and bare "legal"
  were deliberately left out because they appear in nearly every government
  bidding document's standard dispute-resolution and Power-of-Attorney
  clauses, not just actual legal-service tenders. Adjust the list as you
  see real-world results — but avoid single common words.

## Scheduling (macOS `launchd`)

A ready-made job definition is included (`com.mandatebot.dailyrun.plist`,
runs daily at 08:00). To install it:

```bash
cp com.mandatebot.dailyrun.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mandatebot.dailyrun.plist
```

To stop/uninstall:

```bash
launchctl unload ~/Library/LaunchAgents/com.mandatebot.dailyrun.plist
rm ~/Library/LaunchAgents/com.mandatebot.dailyrun.plist
```

Logs from scheduled runs go to `logs/launchd.out.log` / `logs/launchd.err.log`
in addition to the bot's own `logs/run.log`.

## Known limitations

- Category filters are a pre-filter, not a guarantee — legal-adjacent
  tenders can be filed under a generic "Services" category on either
  portal, so results still depend on the keyword pass.
- The BPPT (Balochistan) source runs a real browser per document, which is
  slower than the Punjab source's plain HTTP downloads.
- No email/Slack notification — check `logs/matches.csv` or the
  `downloads/` folder after each run. Ask if you'd like a notification
  step added.
