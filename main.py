import argparse
import logging

from crawler import config
from crawler.ingest import ingest_scope, refresh_known_groups, run_extraction, run_rendering


def parse_scope(args) -> list[tuple[str, str]]:
    pairs = []
    for spec in args.wg:
        tsg, wg_short = spec.split(":", 1)
        if tsg not in config.GROUPS or wg_short not in config.GROUPS[tsg]:
            raise SystemExit(f"Unknown group '{spec}'. Available: {describe_groups()}")
        pairs.append((tsg, wg_short))
    return pairs


def describe_groups() -> str:
    parts = []
    for tsg, wgs in config.GROUPS.items():
        parts.append(", ".join(f"{tsg}:{w}" for w in wgs))
    return "; ".join(parts)


def main():
    parser = argparse.ArgumentParser(description="3GPP TDoc metadata/document crawler")
    parser.add_argument(
        "--wg", action="append", default=[],
        help="Group to crawl, as tsg:wg_short (e.g. ran:RAN2). Repeatable, required unless --extract. "
             f"Available: {describe_groups()}",
    )
    parser.add_argument("--meetings-per-wg", type=int, default=1,
                         help="How many of the most recent meetings to ingest per group (0 = all).")
    parser.add_argument("--download", action="store_true",
                         help="Also download TDoc zip files, not just metadata.")
    parser.add_argument("--max-files", type=int, default=10,
                         help="Safety cap on zip downloads per meeting when --download is set.")
    parser.add_argument("--extract", action="store_true",
                         help="Extract text from downloaded zips that haven't been processed yet, "
                              "instead of crawling.")
    parser.add_argument("--extract-limit", type=int, default=None,
                         help="Cap how many pending TDocs to extract in one run.")
    parser.add_argument("--render", action="store_true",
                         help="Render downloaded zips' main document to PDF (for the web preview) "
                              "that haven't been processed yet, instead of crawling. Requires LibreOffice.")
    parser.add_argument("--render-limit", type=int, default=None,
                         help="Cap how many pending TDocs to render in one run.")
    parser.add_argument("--refresh", action="store_true",
                         help="Daily-refresh mode: re-sync every group already tracked in the DB "
                              "(new meetings, new TDocs, and changed files on already-downloaded "
                              "ones), then extract. Ignores --wg.")
    parser.add_argument("--refresh-max-files", type=int, default=200,
                         help="Cap on downloads per meeting during --refresh (default 200; "
                              "pass a large number or handle backfill separately via --wg/--download "
                              "for a group that isn't fully backfilled yet).")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if not args.verbose else logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.refresh:
        refresh_known_groups(meetings_per_wg=2, max_files=args.refresh_max_files)
        return

    if args.extract:
        run_extraction(limit=args.extract_limit)
        return

    if args.render:
        run_rendering(limit=args.render_limit)
        return

    if not args.wg:
        raise SystemExit("--wg is required unless --extract is set")

    scope = parse_scope(args)
    ingest_scope(
        scope,
        meetings_per_wg=args.meetings_per_wg,
        download=args.download,
        max_files=args.max_files,
    )


if __name__ == "__main__":
    main()
