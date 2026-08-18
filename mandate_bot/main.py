from __future__ import annotations

import argparse
import logging
import os
import tempfile
import time
from datetime import datetime

import yaml

from .logging_utils import append_match_log, slugify
from .matcher import find_matches
from .pdf_utils import download_pdf, extract_text
from .scraper import TenderScraper
from .state import SeenState

log = logging.getLogger("mandate_bot")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_dirs(cfg: dict):
    os.makedirs(cfg["paths"]["download_dir"], exist_ok=True)
    os.makedirs(os.path.dirname(cfg["paths"]["state_file"]), exist_ok=True)
    os.makedirs(os.path.dirname(cfg["paths"]["match_log"]), exist_ok=True)
    os.makedirs(os.path.dirname(cfg["paths"]["run_log"]), exist_ok=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Mandate Bot")
    parser.add_argument(
        "--rescan-last", type=int, default=0, metavar="N",
        help="Force re-download/re-scan of the N most recently listed candidate "
             "tenders, even if already marked as seen (listing order = newest first).",
    )
    parser.add_argument(
        "--source", choices=["all", "punjab", "bppt", "kppra", "epms", "sindh", "pndkp", "adb"], default="all",
        help="Run only one source instead of all seven.",
    )
    return parser.parse_args()


def run():
    args = parse_args()
    cfg = load_config()
    ensure_dirs(cfg)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(cfg["paths"]["run_log"]),
            logging.StreamHandler(),
        ],
    )

    state = SeenState(cfg["paths"]["state_file"])
    keywords = cfg.get("keywords", [])

    match_count = 0
    new_candidates = []
    scraper = None
    src = cfg["source"]
    if args.source in ("all", "punjab"):
        try:
            scraper = TenderScraper(
                base_url=src["base_url"],
                listing_path=src["listing_path"],
                verify_ssl=not src.get("insecure_skip_verify", False),
                request_delay=src.get("request_delay_seconds", 1.5),
                max_pages=src.get("max_pages", 20),
            )
            categories = {c.lower() for c in cfg.get("categories", [])}

            tenders = scraper.fetch_all()
            log.info("Fetched %d tenders total", len(tenders))

            candidates = [t for t in tenders if t.category.lower() in categories]
            log.info("%d tenders match category filter %s", len(candidates), sorted(categories))

            if args.rescan_last > 0:
                forced = candidates[:args.rescan_last]
                for t in forced:
                    state.unmark(t.key)
                log.info("--rescan-last %d: force-unmarked %d candidates for re-scan",
                          args.rescan_last, len(forced))

            new_candidates = [t for t in candidates if not state.has(t.key)]
            log.info("%d are new (not previously processed)", len(new_candidates))
        except Exception:
            log.exception("Punjab source failed, skipping the rest of it and moving on")

    for t in new_candidates:
        urls = [u for u in (t.document_url, t.notice_url) if u]
        if not urls:
            state.mark(t.key)
            continue

        combined_text = ""
        tmp_files = []
        any_failed = False
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                for i, url in enumerate(urls):
                    tmp_path = os.path.join(tmpdir, f"doc_{i}.pdf")
                    ok = download_pdf(scraper.session, url, tmp_path, verify_ssl=scraper.verify_ssl)
                    if ok:
                        tmp_files.append((url, tmp_path))
                        combined_text += "\n" + extract_text(tmp_path)
                    else:
                        any_failed = True
                    time.sleep(src.get("request_delay_seconds", 1.5))

                hits = find_matches(combined_text, keywords)
                if hits:
                    match_count += 1
                    folder_name = f"{t.publish_date.replace(' ', '')}_{slugify(t.title)}".replace("/", "-")
                    dest_dir = os.path.join(cfg["paths"]["download_dir"], folder_name)
                    os.makedirs(dest_dir, exist_ok=True)
                    for url, tmp_path in tmp_files:
                        dest_name = os.path.basename(url)
                        with open(tmp_path, "rb") as src_f, open(os.path.join(dest_dir, dest_name), "wb") as dst_f:
                            dst_f.write(src_f.read())

                    log.info("MATCH: %s (%s) — keywords: %s", t.title, t.department, hits)
                    append_match_log(cfg["paths"]["match_log"], {
                        "found_at": datetime.now().isoformat(timespec="seconds"),
                        "source": t.source,
                        "title": t.title,
                        "department": t.department,
                        "category": t.category,
                        "notice_type": t.notice_type,
                        "publish_date": t.publish_date,
                        "close_date": t.close_date,
                        "matched_keywords": "; ".join(hits),
                        "notice_url": t.notice_url or "",
                        "document_url": t.document_url or "",
                        "saved_dir": dest_dir,
                        "tender_ref": t.tender_ref,
                        "extra_urls": "; ".join(t.extra_urls),
                    })
        except Exception:
            log.exception("Error processing tender %s", t.title)
            continue

        if any_failed:
            log.warning("Leaving %s unmarked (seen) for retry — one or more documents failed to download", t.title)
        else:
            state.mark(t.key)

    bppt_cfg = cfg.get("bppt", {})
    bppt_checked = 0
    if bppt_cfg.get("enabled") and args.source in ("all", "bppt"):
        try:
            from . import bppt as bppt_module

            bppt_tenders = bppt_module.fetch_all(
                base_url=bppt_cfg["base_url"],
                listing_path=bppt_cfg["listing_path"],
                categories=bppt_cfg.get("categories", ["Services"]),
                request_delay=bppt_cfg.get("request_delay_seconds", 1.0),
                max_pages=bppt_cfg.get("max_pages", 30),
            )
            log.info("BPPT: fetched %d candidate tenders", len(bppt_tenders))

            if args.rescan_last > 0:
                forced = bppt_tenders[:args.rescan_last]
                for t in forced:
                    state.unmark(t.key)
                log.info("BPPT: --rescan-last %d: force-unmarked %d candidates for re-scan",
                          args.rescan_last, len(forced))

            new_bppt = [t for t in bppt_tenders if not state.has(t.key)]
            bppt_checked = len(new_bppt)
            log.info("BPPT: %d are new (not previously processed)", bppt_checked)

            bppt_matches = bppt_module.process_candidates(new_bppt, keywords, cfg, state, log)
            match_count += bppt_matches
        except Exception:
            log.exception("BPPT source failed, skipping the rest of it and moving on")

    kppra_cfg = cfg.get("kppra", {})
    kppra_checked = 0
    if kppra_cfg.get("enabled") and args.source in ("all", "kppra"):
        try:
            from . import kppra as kppra_module

            kppra_tenders = kppra_module.fetch_all(
                base_url=kppra_cfg["base_url"],
                listing_path=kppra_cfg["listing_path"],
                categories=kppra_cfg.get("categories", ["Consulting Services", "NON-Consulting Services"]),
                verify_ssl=not kppra_cfg.get("insecure_skip_verify", False),
                request_delay=kppra_cfg.get("request_delay_seconds", 0.5),
                max_pages=kppra_cfg.get("max_pages", 20),
            )
            log.info("KPPRA: fetched %d candidate tenders", len(kppra_tenders))

            if args.rescan_last > 0:
                forced = kppra_tenders[:args.rescan_last]
                for t in forced:
                    state.unmark(t.key)
                log.info("KPPRA: --rescan-last %d: force-unmarked %d candidates for re-scan",
                          args.rescan_last, len(forced))

            new_kppra = [t for t in kppra_tenders if not state.has(t.key)]
            kppra_checked = len(new_kppra)
            log.info("KPPRA: %d are new (not previously processed)", kppra_checked)

            kppra_matches = kppra_module.process_candidates(new_kppra, keywords, cfg, state, log)
            match_count += kppra_matches
        except Exception:
            log.exception("KPPRA source failed, skipping the rest of it and moving on")

    epms_cfg = cfg.get("epms", {})
    epms_checked = 0
    if epms_cfg.get("enabled") and args.source in ("all", "epms"):
        try:
            from . import epms as epms_module

            epms_tenders = epms_module.fetch_all(
                base_url=epms_cfg["base_url"],
                listing_path=epms_cfg["listing_path"],
                categories=epms_cfg.get("categories", ["Consultancy Services", "Non-consultancy Services"]),
                verify_ssl=not epms_cfg.get("insecure_skip_verify", False),
                request_delay=epms_cfg.get("request_delay_seconds", 0.5),
                max_pages=epms_cfg.get("max_pages", 20),
            )
            log.info("EPMS: fetched %d candidate tenders", len(epms_tenders))

            if args.rescan_last > 0:
                forced = epms_tenders[:args.rescan_last]
                for t in forced:
                    state.unmark(t.key)
                log.info("EPMS: --rescan-last %d: force-unmarked %d candidates for re-scan",
                          args.rescan_last, len(forced))

            new_epms = [t for t in epms_tenders if not state.has(t.key)]
            epms_checked = len(new_epms)
            log.info("EPMS: %d are new (not previously processed)", epms_checked)

            epms_matches = epms_module.process_candidates(new_epms, keywords, cfg, state, log)
            match_count += epms_matches
        except Exception:
            log.exception("EPMS source failed, skipping the rest of it and moving on")

    sindh_cfg = cfg.get("sindh", {})
    sindh_checked = 0
    if sindh_cfg.get("enabled") and args.source in ("all", "sindh"):
        try:
            from . import sindh as sindh_module

            sindh_tenders = sindh_module.fetch_all(
                base_url=sindh_cfg["base_url"],
                listing_path=sindh_cfg["listing_path"],
                request_delay=sindh_cfg.get("request_delay_seconds", 1.0),
                max_pages=sindh_cfg.get("max_pages", 10),
            )
            log.info("Sindh: fetched %d candidate tenders", len(sindh_tenders))

            if args.rescan_last > 0:
                forced = sindh_tenders[:args.rescan_last]
                for t in forced:
                    state.unmark(t.key)
                log.info("Sindh: --rescan-last %d: force-unmarked %d candidates for re-scan",
                          args.rescan_last, len(forced))

            new_sindh = [t for t in sindh_tenders if not state.has(t.key)]
            sindh_checked = len(new_sindh)
            log.info("Sindh: %d are new (not previously processed)", sindh_checked)

            sindh_matches = sindh_module.process_candidates(new_sindh, keywords, cfg, state, log)
            match_count += sindh_matches
        except Exception:
            log.exception("Sindh source failed, skipping the rest of it and moving on")

    pndkp_cfg = cfg.get("pndkp", {})
    pndkp_checked = 0
    if pndkp_cfg.get("enabled") and args.source in ("all", "pndkp"):
        try:
            from . import pndkp as pndkp_module

            pndkp_tenders = pndkp_module.fetch_all(
                base_url=pndkp_cfg["base_url"],
                listing_path=pndkp_cfg["listing_path"],
                request_delay=pndkp_cfg.get("request_delay_seconds", 1.0),
                max_pages=pndkp_cfg.get("max_pages", 5),
            )
            log.info("PNDKP: fetched %d candidate tenders", len(pndkp_tenders))

            if args.rescan_last > 0:
                forced = pndkp_tenders[:args.rescan_last]
                for t in forced:
                    state.unmark(t.key)
                log.info("PNDKP: --rescan-last %d: force-unmarked %d candidates for re-scan",
                          args.rescan_last, len(forced))

            new_pndkp = [t for t in pndkp_tenders if not state.has(t.key)]
            pndkp_checked = len(new_pndkp)
            log.info("PNDKP: %d are new (not previously processed)", pndkp_checked)

            pndkp_matches = pndkp_module.process_candidates(new_pndkp, keywords, cfg, state, log)
            match_count += pndkp_matches
        except Exception:
            log.exception("PNDKP source failed, skipping the rest of it and moving on")

    adb_cfg = cfg.get("adb", {})
    adb_checked = 0
    if adb_cfg.get("enabled") and args.source in ("all", "adb"):
        try:
            from . import adb as adb_module

            adb_checked, adb_matches = adb_module.run(
                search_terms=adb_cfg.get("search_terms", ["legal", "law"]),
                cfg=cfg,
                state=state,
                log_=log,
            )
            log.info("ADB: %d checked, %d matches", adb_checked, adb_matches)
            match_count += adb_matches
        except Exception:
            log.exception("ADB source failed, skipping the rest of it and moving on")

    state.save()
    log.info("Run complete. %d new matches found out of %d new candidates checked.",
              match_count, len(new_candidates) + bppt_checked + kppra_checked + epms_checked
              + sindh_checked + pndkp_checked + adb_checked)


if __name__ == "__main__":
    run()
