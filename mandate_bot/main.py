from __future__ import annotations

import argparse
import logging
import os

import yaml

from .state import SeenState

log = logging.getLogger("mandate_bot")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")

ALL_SOURCES = ["punjab", "bppt", "kppra", "epms", "sindh", "pndkp", "adb", "worldbank", "wbgeprocure"]


def _parse_source_list(value: str, arg_name: str) -> set[str]:
    names = {s.strip() for s in value.split(",") if s.strip()}
    unknown = names - set(ALL_SOURCES) - {"all"}
    if unknown:
        raise SystemExit(f"{arg_name}: unknown source(s) {sorted(unknown)} — choices are: all, {', '.join(ALL_SOURCES)}")
    return set(ALL_SOURCES) if "all" in names else names


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
        "--source", default="all",
        help="Comma-separated list of sources to run instead of all nine "
             f"(choices: all, {', '.join(ALL_SOURCES)}). E.g. --source punjab,bppt",
    )
    parser.add_argument(
        "--exclude", default="",
        help="Comma-separated list of sources to skip (applied after --source). "
             "E.g. --source all --exclude punjab,bppt — useful for a CI environment "
             "that can't reach a particular source's network.",
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

    requested = _parse_source_list(args.source, "--source")
    excluded = _parse_source_list(args.exclude, "--exclude") if args.exclude else set()
    active = requested - excluded
    log.info("Active sources this run: %s", sorted(active))

    match_count = 0

    punjab_cfg = cfg["source"]
    punjab_checked = 0
    if "punjab" in active:
        try:
            from . import scraper as punjab_module

            categories = {c.lower() for c in cfg.get("categories", [])}

            tenders = punjab_module.fetch_all(
                base_url=punjab_cfg["base_url"],
                listing_path=punjab_cfg["listing_path"],
                verify_ssl=not punjab_cfg.get("insecure_skip_verify", False),
                request_delay=punjab_cfg.get("request_delay_seconds", 1.5),
                max_pages=punjab_cfg.get("max_pages", 20),
            )
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
            punjab_checked = len(new_candidates)
            log.info("%d are new (not previously processed)", punjab_checked)

            punjab_matches = punjab_module.process_candidates(new_candidates, keywords, cfg, state, log)
            match_count += punjab_matches
        except Exception:
            log.exception("Punjab source failed, skipping the rest of it and moving on")

    bppt_cfg = cfg.get("bppt", {})
    bppt_checked = 0
    if bppt_cfg.get("enabled") and "bppt" in active:
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
    if kppra_cfg.get("enabled") and "kppra" in active:
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
    if epms_cfg.get("enabled") and "epms" in active:
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
    if sindh_cfg.get("enabled") and "sindh" in active:
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
    if pndkp_cfg.get("enabled") and "pndkp" in active:
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
    if adb_cfg.get("enabled") and "adb" in active:
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

    worldbank_cfg = cfg.get("worldbank", {})
    worldbank_checked = 0
    if worldbank_cfg.get("enabled") and "worldbank" in active:
        try:
            from . import worldbank as worldbank_module

            worldbank_tenders = worldbank_module.fetch_all(
                request_delay=worldbank_cfg.get("request_delay_seconds", 0.5),
                page_size=worldbank_cfg.get("page_size", 100),
            )
            log.info("World Bank: fetched %d candidate tenders", len(worldbank_tenders))

            if args.rescan_last > 0:
                forced = worldbank_tenders[:args.rescan_last]
                for t in forced:
                    state.unmark(t.key)
                log.info("World Bank: --rescan-last %d: force-unmarked %d candidates for re-scan",
                          args.rescan_last, len(forced))

            new_worldbank = [t for t in worldbank_tenders if not state.has(t.key)]
            worldbank_checked = len(new_worldbank)
            log.info("World Bank: %d are new (not previously processed)", worldbank_checked)

            worldbank_matches = worldbank_module.process_candidates(new_worldbank, keywords, cfg, state, log)
            match_count += worldbank_matches
        except Exception:
            log.exception("World Bank source failed, skipping the rest of it and moving on")

    wbgeprocure_cfg = cfg.get("wbgeprocure", {})
    wbgeprocure_checked = 0
    if wbgeprocure_cfg.get("enabled") and "wbgeprocure" in active:
        try:
            from . import wbgeprocure as wbgeprocure_module

            wbgeprocure_tenders = wbgeprocure_module.fetch_all()
            log.info("WBGeProcure: fetched %d candidate tenders", len(wbgeprocure_tenders))

            if args.rescan_last > 0:
                forced = wbgeprocure_tenders[:args.rescan_last]
                for t in forced:
                    state.unmark(t.key)
                log.info("WBGeProcure: --rescan-last %d: force-unmarked %d candidates for re-scan",
                          args.rescan_last, len(forced))

            new_wbgeprocure = [t for t in wbgeprocure_tenders if not state.has(t.key)]
            wbgeprocure_checked = len(new_wbgeprocure)
            log.info("WBGeProcure: %d are new (not previously processed)", wbgeprocure_checked)

            wbgeprocure_matches = wbgeprocure_module.process_candidates(new_wbgeprocure, keywords, cfg, state, log)
            match_count += wbgeprocure_matches
        except Exception:
            log.exception("WBGeProcure source failed, skipping the rest of it and moving on")

    state.save()
    log.info("Run complete. %d new matches found out of %d new candidates checked.",
              match_count, punjab_checked + bppt_checked + kppra_checked + epms_checked
              + sindh_checked + pndkp_checked + adb_checked + worldbank_checked + wbgeprocure_checked)


if __name__ == "__main__":
    run()
