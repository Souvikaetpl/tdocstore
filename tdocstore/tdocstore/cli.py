"""Command line for the write path and quick checks.

  tdocstore meeting add RAN2 135 --start 2026-08-24 --end 2026-08-28 --location Maastricht
  tdocstore ingest R2-135 [--limit 50] [--agenda 8.1] [--offline-dir PATH] [--force] [--no-docling]
  tdocstore search R2-135 '"AI/ML model" LCM'
  tdocstore text R2-2604936
  tdocstore find R2-2604936 "functionality-based LCM as the starting point"
  tdocstore stats
"""
from __future__ import annotations
import argparse
import json
import logging
import signal
import sys

from .config import Config
from .ingest import ingest_meeting, register_meeting, sync_group
from .store import Store


def main(argv=None):
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="tdocstore")
    ap.add_argument("--data", default=None, help="data dir (overrides TDOCSTORE_DATA)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("meeting"); ms = m.add_subparsers(dest="mcmd", required=True)
    ma = ms.add_parser("add"); ma.add_argument("group"); ma.add_argument("number")
    for f in ("title", "start", "end", "location", "country"):
        ma.add_argument(f"--{f}")

    ig = sub.add_parser("ingest"); ig.add_argument("meeting_ref")
    ig.add_argument("--limit", type=int); ig.add_argument("--agenda"); ig.add_argument("--offline-dir")
    ig.add_argument("--force", action="store_true"); ig.add_argument("--no-docling", action="store_true")

    sy = sub.add_parser("sync"); sy.add_argument("group"); sy.add_argument("--meeting", action="append", help="meeting_ref(s) to restrict to")
    sy.add_argument("--force", action="store_true"); sy.add_argument("--no-docling", action="store_true")
    se = sub.add_parser("search"); se.add_argument("meeting_ref"); se.add_argument("query"); se.add_argument("--limit", type=int, default=10)
    tx = sub.add_parser("text"); tx.add_argument("tdoc_id")
    fd = sub.add_parser("find"); fd.add_argument("tdoc_id"); fd.add_argument("needle")
    pv = sub.add_parser("prev"); pv.add_argument("tdoc_id"); pv.add_argument("--diff", action="store_true")
    sub.add_parser("stats")
    ls = sub.add_parser("list"); ls.add_argument("meeting_ref"); ls.add_argument("--agenda"); ls.add_argument("--source"); ls.add_argument("--limit", type=int, default=50); ls.add_argument("--offset", type=int, default=0)
    sub.add_parser("agenda").add_argument("meeting_ref")

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(message)s")
    cfg = Config() if a.data is None else Config(data_dir=__import__("pathlib").Path(a.data))
    out = None
    if a.cmd == "meeting" and a.mcmd == "add":
        out = {"meeting_ref": register_meeting(cfg, a.group, a.number, title=a.title, start=a.start, end=a.end, location=a.location, country=a.country)}
    elif a.cmd == "ingest":
        rep = ingest_meeting(cfg, a.meeting_ref, limit=a.limit, only_agenda=a.agenda, offline_dir=a.offline_dir, force=a.force, allow_docling=not a.no_docling)
        out = rep.as_dict()
    elif a.cmd == "sync":
        out = [r.as_dict() for r in sync_group(cfg, a.group, meetings=a.meeting, force=a.force, allow_docling=not a.no_docling)]
    else:
        s = Store(cfg)
        if a.cmd == "search":
            out = s.search(a.meeting_ref, a.query, a.limit)
        elif a.cmd == "text":
            r = s.get_text(a.tdoc_id)
            if r["status"] == "ready":
                sys.stdout.write(r["text"]); return 0
            out = r
        elif a.cmd == "find":
            out = s.find_verbatim(a.tdoc_id, a.needle)
        elif a.cmd == "prev":
            out = s.diff_previous(a.tdoc_id) if a.diff else s.previous_version(a.tdoc_id)
        elif a.cmd == "stats":
            out = s.service_info()
        elif a.cmd == "list":
            out = s.list_tdocs(a.meeting_ref, a.agenda, a.source, a.limit, a.offset)
        elif a.cmd == "agenda":
            out = s.list_agenda_items(a.meeting_ref)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False); sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
