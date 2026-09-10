"""Synthetic but structurally realistic RAN2 TDocs + TDoc_List xlsx, for offline tests.
Mirrors real cover-page layout, heading styles, bold Observation/Proposal lines, a table,
a list, NBSP/curly-quote traps, a zip with two docs, a revised doc (R2-...-r1 via is_revision_of),
and a previous-meeting version of one contribution.
"""
from __future__ import annotations
import zipfile
from pathlib import Path

import openpyxl
from docx import Document

HEADERS = ["TDoc", "Title", "Source", "Contact", "Type", "For", "Agenda item", "Agenda item description",
           "TDoc Status", "Is revision of", "Revised to", "Release", "Spec", "Version", "Related WIs", "CR", "CR category", "Uploaded"]


def _cover(doc, meeting, tdoc, source, title, ai, dfor="Discussion"):
    p = doc.add_paragraph(); r = p.add_run(f"3GPP TSG-RAN WG2 Meeting #{meeting}\t\t\t\t{tdoc}"); r.bold = True
    p = doc.add_paragraph(); r = p.add_run("Maastricht, Netherlands, August 24th – 28th 2026"); r.bold = True
    for k, v in (("Agenda Item", ai), ("Source", source), ("Title", title), ("Document for", dfor)):
        p = doc.add_paragraph(); r = p.add_run(f"{k}: {v}"); r.bold = True


def make_contribution(path: Path, *, meeting, tdoc, source, title, ai, proposals, manual_numbering=False, extra_para=None):
    doc = Document()
    _cover(doc, meeting, tdoc, source, title, ai)
    if manual_numbering:
        doc.add_paragraph("1 Introduction")
    else:
        doc.add_heading("1 Introduction", level=1)
    doc.add_paragraph(f"In the RAN2#{int(meeting)-1} meeting [1], RAN2 has made the related agreements in the AI/ML agenda item.")
    if manual_numbering:
        doc.add_paragraph("2 Discussion")
        doc.add_paragraph("2.1 Model Transfer")
    else:
        doc.add_heading("2 Discussion", level=1)
        doc.add_heading("2.1 Model Transfer", level=2)
    doc.add_paragraph("The size and frequency of AI/ML model transfer, including model transfer resulting from UE mobility, have not been characterised.")
    if extra_para:
        doc.add_paragraph(extra_para)
    p = doc.add_paragraph(); p.add_run("Observation 1: ").bold = True; p.add_run("The size and frequency of ").bold = False
    p.add_run("AI/ML model transfer").bold = True; p.add_run(" have not been characterised.")
    for i, text in enumerate(proposals, 1):
        p = doc.add_paragraph(); r = p.add_run(f"Proposal {i}: {text}"); r.bold = True
    # table with a merged header cell
    t = doc.add_table(rows=3, cols=3)
    t.cell(0, 0).merge(t.cell(0, 2)).text = "Comparison of LCM approaches"
    t.cell(1, 0).text = "Approach"; t.cell(1, 1).text = "Signalling overhead"; t.cell(1, 2).text = "Flexibility"
    t.cell(2, 0).text = "Functionality-based LCM"; t.cell(2, 1).text = "Low"; t.cell(2, 2).text = "Medium"
    doc.add_paragraph("Model-ID based activation", style="List Bullet")
    doc.add_paragraph("Functionality based activation", style="List Bullet")
    if manual_numbering:
        doc.add_paragraph("3 Conclusion")
    else:
        doc.add_heading("3 Conclusion", level=1)
    for i, text in enumerate(proposals, 1):
        p = doc.add_paragraph(); r = p.add_run(f"Proposal {i}: {text}"); r.bold = True
    doc.add_heading("4 References", level=1)
    doc.add_paragraph("[1] Chair notes on RAN2#134 meeting")
    doc.save(str(path))


def build_meeting(docs_dir: Path, *, meeting: str, tdocs: list[dict]):
    """tdocs: list of dicts with keys tdoc, source, title, ai, proposals, and optional flags."""
    docs_dir.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "TDoc_List"
    ws.append([f"TDoc list for RAN2#{meeting}"])  # title row before headers (real files do this sometimes)
    ws.append(HEADERS)
    for t in tdocs:
        fname_docx = docs_dir / f"{t['tdoc']}.docx"
        make_contribution(fname_docx, meeting=meeting, tdoc=t["tdoc"], source=t["source"], title=t["title"], ai=t["ai"],
                          proposals=t["proposals"], manual_numbering=t.get("manual", False), extra_para=t.get("extra"))
        if t.get("loose_docx"):
            pass  # keep .docx uploaded without zip
        else:
            zpath = docs_dir / f"{t['tdoc']}.zip"
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(fname_docx, fname_docx.name)
                if t.get("second_doc"):
                    z.writestr(f"{t['tdoc']}_annex.docx", fname_docx.read_bytes())
            fname_docx.unlink()
        ws.append([t["tdoc"], t["title"], t["source"], "someone@example.com", t.get("type", "discussion"), "Discussion",
                   t["ai"], t.get("ai_desc", ""), t.get("status", "available"), t.get("is_revision_of"), t.get("revised_to"),
                   "Rel-20", None, None, "FS_6G_Radio", None, None, "2026-08-13"])
    # a TDoc in the list with no file on the server
    ws.append([f"R2-26{meeting}99", "Withdrawn contribution", "Nobody", "", "discussion", "Discussion", "9.9", "", "withdrawn",
               None, None, "Rel-20", None, None, None, None, None, "2026-08-13"])
    wb.save(str(docs_dir / f"TDoc_List_Meeting_RAN2#{meeting}.xlsx"))


NBSP_TRAP = "For UE side data collection, RAN shall not decode and interpret the content of the data.\u00a0 RAN shall have awareness on the type of data being transferred."
CURLY_TRAP = "SA2 would like RAN2’s view on “Data transfer to the UE with CN involvement”."


def build_r2_134_and_135(root: Path) -> tuple[Path, Path]:
    d134 = root / "R2-134" / "Docs"; d135 = root / "R2-135" / "Docs"
    build_meeting(d134, meeting="134", tdocs=[
        dict(tdoc="R2-2603833", source="Sharp", title="Discussion on AI/ML LCM and model transfer", ai="9.3.3.2",
             proposals=["RAN2 to discuss coordination between data collection and AI/ML LCM.",
                        "RAN2 to discuss UP vs CP trade-off for model transfer."]),
        dict(tdoc="R2-2603001", source="vivo", title="Data transfer for UE-side model training", ai="9.3.3.1",
             proposals=["RAN2 to take UP DRB as baseline for UE-side data collection."]),
    ])
    build_meeting(d135, meeting="135", tdocs=[
        dict(tdoc="R2-2604936", source="Sharp", title="Discussion on 6G AI/ML LCM Framework", ai="9.3.3.2",
             proposals=["RAN2 to study the size and frequency of AI/ML model transfer, including model transfer triggered by UE mobility.",
                        "For AI/ML model transfer between a UE-side server and the UE, RAN2 to take the existing user plane DRB frameworks as the starting point.",
                        "RAN2 agrees to take functionality-based LCM as the starting point for 6G, while additional support for model-based LCM is not precluded."],
             extra=NBSP_TRAP),
        dict(tdoc="R2-2604573", source="vivo, CAICT, NTT DOCOMO INC.", title="Discussion on data transfer LS from SA2", ai="9.3.3.1",
             proposals=["Reply to SA2 that UC#3 is being considered in RAN2."], extra=CURLY_TRAP, manual=True),
        dict(tdoc="R2-2604675", source="Samsung", title="Multiple PRACH transmissions", ai="8.9.2",
             proposals=["RAN2 to support multiple PRACH transmissions with a single MAC CE."], second_doc=True),
        dict(tdoc="R2-2605500", source="Sharp", title="Discussion on 6G AI/ML LCM Framework", ai="9.3.3.2",
             proposals=["RAN2 agrees to take functionality-based LCM as the starting point for 6G."],
             is_revision_of="R2-2604936", loose_docx=True),
    ])
    return d134, d135
