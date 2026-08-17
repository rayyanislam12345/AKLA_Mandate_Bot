"""Regenerates dashboard.html from logs/matches.csv + whatever's actually
still present in downloads/. Run this any time after a bot run to refresh
the browsable list of matched tenders.

    python3 generate_dashboard.py

Then open dashboard.html in a browser (double-click it, or `open dashboard.html`
on macOS). It's a single self-contained file — no server needed; the
document links are relative paths straight to the PDFs in downloads/.
"""
from __future__ import annotations

import csv
import json
import os

CSV_PATH = "logs/matches.csv"
OUTPUT_PATH = "dashboard.html"


def load_matches() -> list[dict]:
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def resolve_files(saved_dir: str) -> list[str] | None:
    if not saved_dir or not os.path.isdir(saved_dir):
        return None
    files = sorted(
        f for f in os.listdir(saved_dir)
        if not f.startswith(".") and os.path.isfile(os.path.join(saved_dir, f))
    )
    return [os.path.join(saved_dir, f).replace(os.sep, "/") for f in files] if files else None


def build_records() -> list[dict]:
    by_dir: dict[str, dict] = {}
    for row in load_matches():
        saved_dir = row.get("saved_dir", "")
        files = resolve_files(saved_dir)
        if not files:
            continue  # folder missing/emptied since the CSV row was written — not "currently stored"

        record = {
            "found_at": row.get("found_at", ""),
            "source": row.get("source", ""),
            "tender_ref": row.get("tender_ref") or "",
            "title": row.get("title", "").strip(),
            "department": row.get("department", "").strip(),
            "category": row.get("category", ""),
            "notice_type": row.get("notice_type", ""),
            "publish_date": row.get("publish_date", ""),
            "close_date": row.get("close_date", ""),
            "matched_keywords": [k.strip() for k in (row.get("matched_keywords") or "").split(";") if k.strip()],
            "files": files,
        }
        # A tender can appear more than once (e.g. --rescan-last re-matched
        # it) — keep only the most recent entry for a given folder.
        existing = by_dir.get(saved_dir)
        if existing is None or record["found_at"] > existing["found_at"]:
            by_dir[saved_dir] = record

    records = list(by_dir.values())
    records.sort(key=lambda r: r["found_at"], reverse=True)
    return records


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Mandate Bot — Matched Tenders</title>
<style>
  :root {
    --bg: #f7f7f8;
    --panel: #ffffff;
    --border: #e2e2e6;
    --text: #1b1b1f;
    --muted: #6b6b74;
    --accent: #2f5fda;
    --accent-bg: #eaf0ff;
    --row-alt: #fafafb;
    --tag-bg: #fff3cd;
    --tag-text: #7a5c00;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
  }
  header {
    padding: 20px 28px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
  }
  h1 {
    margin: 0 0 4px 0;
    font-size: 20px;
    font-weight: 650;
  }
  .subtitle {
    color: var(--muted);
    font-size: 13px;
  }
  .controls {
    display: flex;
    gap: 10px;
    padding: 14px 28px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
    align-items: center;
  }
  .controls input, .controls select {
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 13px;
    background: var(--bg);
    color: var(--text);
  }
  .controls input { flex: 1; min-width: 200px; }
  .count-badge {
    font-size: 12px;
    color: var(--muted);
    margin-left: auto;
  }
  main { padding: 20px 28px 40px; }
  .table-wrap {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: auto;
    max-height: calc(100vh - 170px);
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  thead th {
    position: sticky;
    top: 0;
    background: var(--panel);
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
  }
  thead th:hover { color: var(--accent); }
  thead th.sorted::after { content: " " attr(data-dir); font-size: 10px; color: var(--accent); }
  tbody tr:nth-child(even) { background: var(--row-alt); }
  tbody tr:hover { background: var(--accent-bg); }
  td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }
  td.title-cell { min-width: 260px; max-width: 420px; }
  .title-main {
    font-weight: 550;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
    cursor: help;
  }
  .dept {
    color: var(--muted);
    font-size: 12px;
    margin-top: 2px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    cursor: help;
  }
  .source-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    background: var(--accent-bg);
    color: var(--accent);
    white-space: nowrap;
  }
  .kw {
    display: inline-block;
    background: var(--tag-bg);
    color: var(--tag-text);
    border-radius: 5px;
    padding: 1px 6px;
    font-size: 11px;
    margin: 1px 3px 1px 0;
    white-space: nowrap;
  }
  .docs a {
    display: block;
    color: var(--accent);
    text-decoration: none;
    font-size: 12px;
    margin-bottom: 3px;
    white-space: nowrap;
  }
  .docs a:hover { text-decoration: underline; }
  .empty-state {
    padding: 60px 20px;
    text-align: center;
    color: var(--muted);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17181c;
      --panel: #1e1f24;
      --border: #2c2d33;
      --text: #e7e7ea;
      --muted: #9a9aa3;
      --accent: #7fa1ff;
      --accent-bg: #1f2740;
      --row-alt: #202127;
      --tag-bg: #3a3312;
      --tag-text: #e8c86a;
    }
  }
</style>
</head>
<body>
<header>
  <h1>Mandate Bot — Matched Tenders</h1>
  <div class="subtitle">Legal-services tenders found across Punjab, Balochistan (BPPT), KPPRA, and federal PPRA (EPMS)</div>
</header>
<div class="controls">
  <input id="search" type="text" placeholder="Search title, department, keywords...">
  <select id="sourceFilter">
    <option value="">All sources</option>
  </select>
  <span class="count-badge" id="countBadge"></span>
</div>
<main>
  <div class="table-wrap">
    <table id="tbl">
      <thead>
        <tr>
          <th data-key="source">Source</th>
          <th data-key="tender_ref">Ref</th>
          <th data-key="title">Title / Department</th>
          <th data-key="category">Category</th>
          <th data-key="publish_date">Published</th>
          <th data-key="close_date">Closing</th>
          <th data-key="matched_keywords">Matched Keywords</th>
          <th data-key="found_at">Found</th>
          <th>Documents</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</main>
<script>
const DATA = __DATA__;

const tbody = document.getElementById('tbody');
const searchInput = document.getElementById('search');
const sourceFilter = document.getElementById('sourceFilter');
const countBadge = document.getElementById('countBadge');

const sources = [...new Set(DATA.map(r => r.source))].sort();
for (const s of sources) {
  const opt = document.createElement('option');
  opt.value = s;
  opt.textContent = s;
  sourceFilter.appendChild(opt);
}

let sortKey = 'found_at';
let sortDir = -1;

function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fileLabel(path) {
  const name = path.split('/').pop();
  return name.length > 28 ? name.slice(0, 25) + '...' : name;
}

function render() {
  const q = searchInput.value.trim().toLowerCase();
  const src = sourceFilter.value;

  let rows = DATA.filter(r => {
    if (src && r.source !== src) return false;
    if (!q) return true;
    const hay = [r.title, r.department, r.tender_ref, ...(r.matched_keywords || [])].join(' ').toLowerCase();
    return hay.includes(q);
  });

  rows.sort((a, b) => {
    let av = a[sortKey] || '', bv = b[sortKey] || '';
    if (Array.isArray(av)) av = av.join(',');
    if (Array.isArray(bv)) bv = bv.join(',');
    return av < bv ? -sortDir : av > bv ? sortDir : 0;
  });

  countBadge.textContent = `${rows.length} of ${DATA.length} matched tenders`;

  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9"><div class="empty-state">No matches found.</div></td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(r => `
    <tr>
      <td><span class="source-badge">${escapeHtml(r.source)}</span></td>
      <td>${escapeHtml(r.tender_ref)}</td>
      <td class="title-cell">
        <div class="title-main" title="${escapeHtml(r.title)}">${escapeHtml(r.title)}</div>
        <div class="dept" title="${escapeHtml(r.department)}">${escapeHtml(r.department)}</div>
      </td>
      <td>${escapeHtml(r.category)}</td>
      <td>${escapeHtml(r.publish_date)}</td>
      <td>${escapeHtml(r.close_date)}</td>
      <td>${(r.matched_keywords || []).map(k => `<span class="kw">${escapeHtml(k)}</span>`).join('')}</td>
      <td>${escapeHtml(r.found_at)}</td>
      <td class="docs">${r.files.map(f => `<a href="${encodeURI(f)}" target="_blank">${escapeHtml(fileLabel(f))}</a>`).join('')}</td>
    </tr>
  `).join('');
}

document.querySelectorAll('thead th[data-key]').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.key;
    if (sortKey === key) {
      sortDir *= -1;
    } else {
      sortKey = key;
      sortDir = 1;
    }
    document.querySelectorAll('thead th').forEach(h => h.classList.remove('sorted'));
    th.classList.add('sorted');
    th.setAttribute('data-dir', sortDir === 1 ? '▲' : '▼');
    render();
  });
});

searchInput.addEventListener('input', render);
sourceFilter.addEventListener('change', render);
render();
</script>
</body>
</html>
"""


def main():
    records = build_records()
    data_json = json.dumps(records, ensure_ascii=False)
    html_out = TEMPLATE.replace("__DATA__", data_json)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"Wrote {OUTPUT_PATH} with {len(records)} matched tenders")


if __name__ == "__main__":
    main()
