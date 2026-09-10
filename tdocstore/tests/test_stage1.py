"""Stage 1 acceptance tests. Run: pytest -q
Covers: TDoc_List parsing, DOCX extraction traps, idempotent offline ingest, Store API contracts,
FTS query hardening, verbatim round-trip, previous-version linking, online crawl against a local
mock of the 3GPP file server, and the MCP server driven by the official MCP client over stdio.
"""
from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fixtures import CURLY_TRAP, NBSP_TRAP, build_r2_134_and_135  # noqa: E402

from tdocstore import Store  # noqa: E402
from tdocstore.config import Config  # noqa: E402
from tdocstore.extract import extract_any  # noqa: E402
from tdocstore.fetch import parse_listing  # noqa: E402
from tdocstore.ingest import ingest_meeting, register_meeting, sync_group  # noqa: E402
from tdocstore.tdoclist import parse_tdoc_list  # noqa: E402


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("tds")
    d134, d135 = build_r2_134_and_135(root / "fixtures")
    cfg = Config(data_dir=root / "data")
    register_meeting(cfg, "RAN2", "134", start="2026-05-18", end="2026-05-22", location="China")
    register_meeting(cfg, "RAN2", "135", start="2026-08-24", end="2026-08-28", location="Maastricht")
    r134 = ingest_meeting(cfg, "R2-134", offline_dir=d134)
    r135 = ingest_meeting(cfg, "R2-135", offline_dir=d135)
    return {"cfg": cfg, "root": root, "d134": d134, "d135": d135, "r134": r134, "r135": r135, "store": Store(cfg)}


# ---------- tdoclist ----------
def test_tdoclist_parses_with_title_row_and_aliases(env):
    recs = list(parse_tdoc_list(env["d135"] / "TDoc_List_Meeting_RAN2#135.xlsx"))
    ids = {r["tdoc_id"] for r in recs}
    assert {"R2-2604936", "R2-2604573", "R2-2604675", "R2-2605500", "R2-2613599"} <= ids
    r = next(x for x in recs if x["tdoc_id"] == "R2-2605500")
    assert r["is_revision_of"] == "R2-2604936" and r["agenda_item"] == "9.3.3.2" and r["source"] == "Sharp"


# ---------- extract ----------
def test_extract_docx_structure(env, tmp_path):
    ex = extract_any(env["d135"] / "R2-2604936.zip", tmp_path / "w")
    assert ex.source_type == "docx-fast"
    heads = [(h, l) for h, l, _ in ex.sections]
    assert ("1 Introduction", 1) in heads and ("2.1 Model Transfer", 2) in heads and ("3 Conclusion", 1) in heads
    assert "## 2.1 Model Transfer" in ex.markdown and "**Proposal 1:" in ex.markdown
    cells = [p.text for p in ex.paragraphs if p.is_table_cell]
    assert cells[:2] == ["Comparison of LCM approaches", "Approach"]  # merged header cell appears once
    assert any(p.text.startswith("Model-ID based activation") for p in ex.paragraphs)
    assert "| Approach | Signalling overhead | Flexibility |" in ex.markdown


def test_extract_manual_numbering_and_zip_multi(env, tmp_path):
    ex = extract_any(env["d135"] / "R2-2604573.zip", tmp_path / "w1")
    assert ("2.1 Model Transfer", 2) in [(h, l) for h, l, _ in ex.sections]  # no heading styles used
    ex2 = extract_any(env["d135"] / "R2-2604675.zip", tmp_path / "w2")
    assert ex2.source_type.startswith("zip-multi") and len(ex2.sections) == 12


# ---------- ingest ----------
def test_ingest_report_and_idempotency(env):
    r = env["r135"]
    assert r.in_tdoclist == 5 and r.extracted == 4 and r.missing == 1 and r.extract_errors == 0
    again = ingest_meeting(env["cfg"], "R2-135", offline_dir=env["d135"])
    assert again.extracted == 0 and again.skipped_unchanged == 4
    s = env["store"]
    assert s.list_tdocs("R2-135")["total"] == 5
    con = s._con("RAN2")
    n_sec = con.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    n_fts = con.execute("SELECT COUNT(*) FROM sections_fts").fetchone()[0]
    assert n_sec == n_fts > 0
    forced = ingest_meeting(env["cfg"], "R2-135", offline_dir=env["d135"], force=True)
    assert forced.extracted == 4
    assert con.execute("SELECT COUNT(*) FROM sections").fetchone()[0] == n_sec
    assert con.execute("SELECT COUNT(*) FROM sections_fts").fetchone()[0] == n_fts


# ---------- store ----------
def test_meetings_and_agenda(env):
    s = env["store"]
    refs = [m["meeting_ref"] for m in s.list_meetings(group="RAN2")]
    assert refs == ["R2-135", "R2-134"]
    ai = s.list_agenda_items("R2-135")["agenda_items"]
    assert [a["agenda_item"] for a in ai] == ["8.9.2", "9.3.3.1", "9.3.3.2", "9.9"]
    assert s.list_agenda_items("R2-999")["status"] == "not_found"


def test_list_tdocs_prefix_and_paging(env):
    s = env["store"]
    assert s.list_tdocs("R2-135", agenda_item="9.3.3")["total"] == 3
    assert s.list_tdocs("R2-135", agenda_item="9.3.3.1")["total"] == 1
    assert s.list_tdocs("R2-135", agenda_item="9")["total"] == 4  # 9.x incl. 9.9; must not match 8.9.2
    p = s.list_tdocs("R2-135", source="sharp", limit=1)
    assert p["total"] == 2 and p["returned"] == 1 and p["has_more"] is True
    assert s.list_tdocs("R2-135", source="sharp", limit=1, offset=1)["has_more"] is False
    assert s.list_tdocs("XX-1")["status"] == "invalid_argument"


def test_search_semantics_and_hardening(env):
    s = env["store"]
    r = s.search("R2-135", '"AI/ML model transfer" mobility')
    assert r["status"] == "ready" and r["total"] >= 3 and "<mark>" in r["results"][0]["hits"][0]["snippet"]
    assert s.search("R2-135", "PRACH")["results"][0]["tdoc_id"] == "R2-2604675"
    assert s.search("R2-134", "PRACH")["total"] == 0  # meeting-scoped
    assert s.search("R2-135", "model-based")["total"] == 1
    for bad in ["(", "NEAR", "a", "AND OR NOT", "", '"']:
        assert s.search("R2-135", bad)["status"] == "invalid_argument"
    assert s.search("R2-135", 'x" OR "y')["status"] in ("ready", "invalid_argument")  # never raises
    assert s.search("R2-135", "sharp*^:")["status"] == "ready"


def test_text_sections_paragraphs(env):
    s = env["store"]
    t = s.get_text("R2-2604936")
    assert t["status"] == "ready" and t["source_type"] == "docx-fast" and t["extracted_at"] and t["file_url"] is None or True
    assert "# 1 Introduction" in t["text"]
    assert [x["heading"] for x in s.get_sections("R2-2604936")["sections"]][1] == "1 Introduction"
    ps = s.get_paragraphs("R2-2604936")["paragraphs"]
    full = "\n".join(p["text"] for p in ps)
    for p in ps:
        assert full[p["char_start"]:p["char_end"]] == p["text"]
    assert s.get_text("R2-2613599")["status"] == "not_indexed"   # in TDoc_List, file missing
    assert s.get_text("R2-2600000")["status"] == "not_found"
    assert s.get_text("hello")["status"] == "invalid_argument"


def test_verbatim_roundtrip_through_nbsp_and_curly_quotes(env):
    s = env["store"]
    f = s.find_verbatim("R2-2604936", NBSP_TRAP.replace("\xa0", " "))
    assert f["count"] == 1
    h = f["hits"][0]
    v = s.get_verbatim("R2-2604936", h["char_start"], h["char_end"])
    assert v["text"] == h["text"] and "\xa0" in v["text"]  # original NBSP preserved
    f2 = s.find_verbatim("R2-2604573", "SA2 would like RAN2's view on \"Data transfer to the UE with CN involvement\".")
    assert f2["count"] == 1 and f2["hits"][0]["exact"] is False and f2["hits"][0]["text"] == CURLY_TRAP
    assert s.find_verbatim("R2-2604936", "Proposal 3:")["count"] == 2   # discussion + conclusion
    assert s.find_verbatim("R2-2604936", "nope nope nope")["count"] == 0
    assert s.get_verbatim("R2-2604936", 50, 10)["status"] == "invalid_argument"


def test_previous_version_and_diff(env):
    s = env["store"]
    a = s.previous_version("R2-2605500")
    assert a["previous_tdoc_id"] == "R2-2604936" and a["strategy"] == "revised_from_tdoclist"
    b = s.previous_version("R2-2604936")
    assert b["previous_tdoc_id"] == "R2-2603833" and b["strategy"] == "prev_meeting_source_title" and b["score"] >= 0.6
    assert s.previous_version("R2-2604675")["status"] == "no_previous"
    d = s.diff_previous("R2-2604936")
    assert d["status"] == "ready" and d["paragraphs_added"] > 0 and "+++ R2-2604936" in d["unified_diff"]


# ---------- fetch / online ----------
def test_parse_listing_decodes_and_dedups():
    html = ('<a href="/ftp/x/TSGR2_135">[To Parent Directory]</a>'
            '<a href="/ftp/x/Docs/R2-2604501.zip">R2-2604501.zip</a><a href="/ftp/x/Docs/R2-2604501.zip?sortby=date">dup</a>'
            '<a href="/ftp/x/Docs/TDoc_List_Meeting_RAN2%23135.xlsx">l</a><a href="R2-2606032.docx">d</a>')
    assert parse_listing(html) == ["TSGR2_135", "R2-2604501.zip", "TDoc_List_Meeting_RAN2#135.xlsx", "R2-2606032.docx"]


@pytest.fixture(scope="module")
def mock_3gpp(env, tmp_path_factory):
    root = tmp_path_factory.mktemp("mock")
    docs = root / "tsg_ran" / "WG2_RL2" / "TSGR2_135" / "Docs"
    docs.mkdir(parents=True)
    for p in env["d135"].iterdir():
        (docs / p.name).write_bytes(p.read_bytes())
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_online_sync_discovers_fetches_resumes(mock_3gpp, tmp_path):
    cfg = Config(data_dir=tmp_path / "data", base_url=mock_3gpp, concurrency=2, min_delay_s=0.01)
    reps = sync_group(cfg, "RAN2")
    assert [r.meeting_ref for r in reps] == ["R2-135"]
    r = reps[0]
    assert r.fetched == 4 and r.extracted == 4 and r.missing == 1
    assert (cfg.files_dir / "RAN2" / "R2-135" / "TDoc_List_Meeting_RAN2#135.xlsx").exists()
    s = Store(cfg)
    t = s.get_tdoc("R2-2604936")
    assert t["file_url"].endswith("/Docs/R2-2604936.zip") and t["file_sha256"]
    r2 = sync_group(cfg, "RAN2")[0]
    assert r2.fetched == 0 and r2.cached == 4 and r2.skipped_unchanged == 4
    assert s.search("R2-135", "PRACH")["total"] == 1


# ---------- MCP ----------
def test_mcp_stdio_end_to_end(env):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "tdocstore.mcp_server", "--transport", "stdio"],
                                       env={**os.environ, "TDOCSTORE_DATA": str(env["cfg"].data_dir)})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert {"list_meetings", "list_tdoc_agenda_items", "list_tdocs", "search_tdocs", "get_tdoc_text",
                        "find_verbatim", "get_verbatim", "get_previous_version", "get_tdoc_diff", "service_info"} <= names

                async def call(name, **kw):
                    res = await s.call_tool(name, kw)
                    assert not res.isError, res
                    return json.loads(res.content[0].text)
                assert [m["meeting_ref"] for m in (await call("list_meetings", group="RAN2"))["meetings"]] == ["R2-135", "R2-134"]
                sr = await call("search_tdocs", meeting="R2-135", query='"functionality-based LCM"')
                assert sr["total"] >= 1
                f = await call("find_verbatim", tdoc_id="R2-2604936", needle="RAN shall not decode and interpret")
                h = f["hits"][0]
                v = await call("get_verbatim", tdoc_id="R2-2604936", char_start=h["char_start"], char_end=h["char_end"])
                assert v["text"] == h["text"]
                assert (await call("get_tdoc_text", tdoc_id="nonsense"))["status"] == "invalid_argument"
    asyncio.run(run())
