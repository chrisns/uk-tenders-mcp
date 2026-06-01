"""CLI entrypoint.

Examples:
  # validate against live FTS, no BigQuery:
  python -m uk_tenders_ingest --source fts --mode backfill \
      --from 2026-05-01 --to 2026-05-31 --dry-run --max-releases 200

  # real load (needs GCP creds + datasets):
  python -m uk_tenders_ingest --source fts --mode nightly --from 2026-05-29 --to 2026-05-31
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

from .config import ALL_SOURCES, SOURCE_FTS, SOURCES, Settings
from .pipeline import run
from .scrape_ingest import SCRAPE_RUNNERS


def _default_nightly_window(settings: Settings) -> tuple[str, str]:
    now = datetime.utcnow()
    frm = now - timedelta(minutes=settings.nightly_lookback_minutes) - timedelta(days=1)
    return frm.strftime("%Y-%m-%dT%H:%M:%S"), now.strftime("%Y-%m-%dT%H:%M:%S")


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    p = argparse.ArgumentParser(prog="uk-tenders-ingest")
    p.add_argument("--source", default=SOURCE_FTS, choices=[*ALL_SOURCES, "all"])
    p.add_argument("--exclude", default="", help="comma-separated sources to skip (with --source all)")
    p.add_argument("--mode", default="nightly", choices=["backfill", "nightly"])
    p.add_argument("--match", action="store_true", help="run cross-source dedup after ingestion")
    p.add_argument("--from", dest="frm", help="ISO date/datetime (default: source floor for backfill)")
    p.add_argument("--to", dest="to", help="ISO date/datetime (default: now)")
    p.add_argument("--dry-run", action="store_true", help="no BigQuery; validate parse+compile")
    p.add_argument("--max-windows", type=int, default=None)
    p.add_argument("--max-releases", type=int, default=None)
    p.add_argument("--bootstrap", action="store_true", help="create datasets + tables first")
    args = p.parse_args(argv)

    loader = None
    if not args.dry_run:
        from .bq import BigQueryLoader

        loader = BigQueryLoader(
            settings.project, settings.raw_dataset, settings.public_dataset, settings.bq_location
        )
        if args.bootstrap:
            import pathlib

            loader.ensure_datasets()
            ddl = (pathlib.Path(__file__).resolve().parents[3] / "sql" / "schema.sql").read_text()
            loader.run_ddl(ddl)
            print("bootstrap: datasets + tables ensured")

    # `all` runs every source. OCDS sources go through pipeline.run; the scrape sources
    # (Sell2Wales, eTendersNI) go through scrape_ingest — Sell2Wales because its upstream OCDS
    # API is broken, eTendersNI because it's CAPTCHA-gated (solved by Gemini, no human).
    sources = list(ALL_SOURCES) if args.source == "all" else [args.source]
    excluded = {s.strip() for s in args.exclude.split(",") if s.strip()}
    sources = [s for s in sources if s not in excluded]
    summaries: list = []
    for src in sources:
        if args.mode == "backfill":
            frm = args.frm or SOURCES[src].backfill_floor
            to = args.to or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        else:
            d_from, d_to = _default_nightly_window(settings)
            frm = args.frm or d_from
            to = args.to or d_to
        try:
            if src in SCRAPE_RUNNERS:
                if args.dry_run:
                    summaries.append({"source": src, "skipped": "scrape source not run in --dry-run"})
                    continue
                summaries.append(SCRAPE_RUNNERS[src](settings, loader, frm, to, args.mode))
            else:
                summary = run(
                    source=src, mode=args.mode, window_from=frm, window_to=to,
                    settings=settings, loader=loader, dry_run=args.dry_run,
                    max_windows=args.max_windows, max_releases=args.max_releases,
                )
                summary.pop("sample_compiled", None)
                summaries.append(summary)
        except Exception as exc:  # adapter isolation (ADR-0004): one source down ≠ all down
            print(f"[{src}] FAILED (isolated): {exc}")
            summaries.append({"source": src, "error": str(exc)})

    if args.match and not args.dry_run:
        from .match import run as match_run

        summaries.append({"match": match_run(
            settings.project, settings.raw_dataset, settings.public_dataset, settings.bq_location
        )})

    print(json.dumps(summaries, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
