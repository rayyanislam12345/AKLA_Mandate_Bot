# Mandate Bot

Scrapes active tenders/opportunities from seven procurement sources
(domestic Pakistani portals plus one international one), downloads
documents (plus any corrigenda/addenda/minutes) for anything tagged as a
legal-adjacent procurement type, scans the text for legal-services
keywords, saves matches into `downloads/`, and generates a browsable local
dashboard (`dashboard.html`) of everything found.

Sources:
- **Punjab (Pakistan) e-Procurement portal** (`eproc.punjab.gov.pk`)
- **Balochistan Public Procurement Regulatory Authority** (`bpptwo.vdc.services`)
- **Khyber Pakhtunkhwa Public Procurement Regulatory Authority** (`kppra.gov.pk`)
- **Federal PPRA e-Publish & Monitoring System** (`epms.ppra.gov.pk`)
- **Sindh Public Procurement Regulatory Authority** (`ppms.pprasindh.gov.pk`)
- **KP Planning & Development Department** (`pndkp.gov.pk`)
- **Asian Development Bank Consultant Management System** (`selfservice.adb.org`) — international-tier

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

### KPPRA (`mandate_bot/kppra.py`)
Classic server-rendered PHP with query-string pagination — plain `requests`,
no browser needed. The listing table doesn't show a category, so a small
AJAX JSON endpoint is called once per listed tender to classify it (and,
when present, to pick up corrigendum/addendum/meeting-minutes documents the
listing table never shows at all). Documents are a mix of real PDFs and
photographed/scanned JPGs of physical notice boards; both go through the
same OCR-capable text extraction as the Punjab source.

### EPMS / federal PPRA (`mandate_bot/epms.py`)
Also classic server-rendered pages, plain `requests`. Unlike KPPRA, its
category filter (`procurement_category`) actually works server-side, so
only real Consultancy/Non-consultancy Services candidates get fetched
instead of classifying every tender individually. Each tender's detail page
is scanned for every `/pdf?file=...` link present (not just a fixed pair),
so corrigendum documents are picked up automatically whenever they exist.

### Sindh (`mandate_bot/sindh.py`)
A JSF/PrimeFaces app (Java, session/ViewState-based) — every document
download is a stateful form POST (`PrimeFaces.addSubmitParam(...).submit(...)`),
not a plain link, so like BPPT this source is scraped with Playwright. No
procurement-category taxonomy is exposed at all, but only a small handful of
this portal's ~2800 total tenders are ever Status=Active at a given time
(the rest are Archived/Cancelled history), so every Active tender is
scanned rather than needing a category pre-filter. Each tender can bundle
several items/schemes, each with its own Bidding Document — all of them are
downloaded and matched. Committee-approval documents and newspaper-ad scans
are deliberately skipped (administrative noise / same false-positive risk
as EPMS's advertisement docs, respectively).

### KP Planning & Development Department (`mandate_bot/pndkp.py`)
The simplest source of the seven: a WordPress site (WP Download Manager
plugin) where the title and a directly downloadable file URL are both right
there in the listing page — no detail-page visit, no browser, no pagination
(the whole tenders category is ~39 files on one page). No department,
category, or closing-date metadata is exposed (it's a generic file library,
not a purpose-built tender portal), so every listed file is scanned
directly. This source is also where the mixed-filetype gap was found and
fixed — its "documents" are a genuine mix of PDFs, scanned JPG/PNG images,
and Word `.docx` files, not just PDFs like every other source assumed.
`pdf_utils.extract_text_any()` (shared with KPPRA) now branches on the
downloaded file's actual signature: PDF → existing pdfplumber+OCR pipeline,
`.docx` → `python-docx`, anything else → OCR directly as an image.

### Asian Development Bank (`mandate_bot/adb.py`)
An Oracle E-Business Suite / OA Framework app — a different kind of source
in two ways. First, it's international-tier (loans/grants/TA consulting
opportunities across ADB member countries), not a domestic Pakistani
portal. Second, although the URL is a login-capable "self service" portal
and credentials were provided for it, **they're intentionally unused**:
browsing, searching, viewing full opportunity details, and downloading
Terms of Reference are all public — login is only needed to actually
submit a bid ("Express Interest"), which this bot never does. If that ever
changes, credentials live in `secrets.yaml` (gitignored, never committed).

Matching also works differently here: the site's "Search by Expertise"
box searches a tag field curated by ADB staff, not generic document text,
so every result returned by the `adb.search_terms` searches (`legal`,
`law` by default) is treated as a match directly — no local keyword pass,
since the existing Pakistan-tuned keyword list doesn't match ADB's
phrasing style (e.g. "Legal Expert" vs. "hiring of legal") and would
otherwise hide genuinely relevant results. The "Terms of Reference" tab
always has the full content rendered inline regardless of whether a
separate file attachment also exists, so it's always captured as a PDF
snapshot (same `page.pdf()` pattern as BPPT); any real attachment file is
downloaded as well when present.

Since the search box can't look up a specific opportunity by ID afterward,
this source runs fetch and document-capture as one pass per search term
(already-seen titles are skipped before ever clicking into them, so
re-runs stay cheap) rather than the two-phase split every other source
uses.

### All sources
The combined text is checked against the keyword list in `config.yaml`. If
any keyword hits, every document found for that tender (primary + any
corrigenda/addenda/minutes) is saved into `downloads/<publish-date>_<title>/`
and a row is appended to `logs/matches.csv`. Every tender checked (match or
not) is recorded in `state/seen.json` so re-runs only process newly
published tenders — and that state is saved after every single tender, not
just at the end, so a long run can be safely interrupted and resumed later.

## Browsing matches (`dashboard.html`)

```bash
python3 generate_dashboard.py
open dashboard.html   # macOS; or just double-click the file
```

Regenerates a single self-contained HTML file from `logs/matches.csv` +
whatever's actually still present in `downloads/` (rows whose folder was
deleted are skipped, so it always reflects what's really on disk). No
server needed — it's opened directly in a browser via `file://`, with
document links as relative paths straight to the PDFs.

Shows: source portal, tender reference number (when the portal exposes
one), title, department, category, publish/closing dates, which keywords
matched, when it was found, and download links for every saved document.
Has a search box (matches title/department/keywords/ref) and a source
filter; click any column header to sort.

Re-run it any time after a bot run to refresh — it's cheap and instant
(no network calls, just reads the CSV and the downloads folder).

## Setup

```bash
cd "Mandate Bot"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium   # one-time browser download (~300MB), needed for BPPT/Sindh/ADB
```

Also needs, via Homebrew (for OCR on Punjab/KPPRA/PNDKP):
```bash
brew install tesseract poppler
```

If any source ever needs login credentials (currently none do — see
`mandate_bot/adb.py`'s docstring), put them in `secrets.yaml` at the project
root (gitignored, never committed — see the template comment at the top of
that file if you need to create it).

## Run manually

```bash
./run.sh
# or: source .venv/bin/activate && python3 -m mandate_bot.main
```

A full run (all seven sources) currently takes on the order of tens of
minutes to a few hours depending on network conditions, since every
candidate document is downloaded/rendered and read individually with a
polite delay between requests — this is normal, let it finish. Each source
saves its progress incrementally, so an interrupted run can be safely
resumed with `./run.sh` again.

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
- `source.max_pages` / `bppt.max_pages` / `kppra.max_pages` /
  `epms.max_pages` — safety cap on how many grid pages to walk per run, per
  source. BPPT's Services category alone runs to 150+ pages, so its cap is
  set much higher (250) than the others.
- `categories` (Punjab) / `bppt.categories` / `kppra.categories` /
  `epms.categories` — which listing categories to bother
  downloading/scanning, per source. Balochistan's and EPMS's are proper
  taxonomies with working server-side filters; Punjab's and KPPRA's are
  cruder and/or require per-tender classification (see each source's
  section above).
- `keywords` — case-insensitive substring match against extracted document
  text, shared by all sources. Tuned to avoid boilerplate false positives:
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
