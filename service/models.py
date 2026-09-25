from dataclasses import dataclass
from typing import Optional


@dataclass
class TDocSummary:
    tdoc_id: str
    title: Optional[str]
    source: Optional[str]
    doc_type: Optional[str]
    status: Optional[str]
    tsg: str
    wg_short: str
    meeting_folder: str
    specification: Optional[str]
    uploaded_at: Optional[str]


@dataclass
class TDocDetail(TDocSummary):
    contact: Optional[str] = None
    for_action: Optional[str] = None
    abstract: Optional[str] = None
    agenda_item: Optional[str] = None
    agenda_item_description: Optional[str] = None
    release: Optional[str] = None
    spec_version: Optional[str] = None
    related_wis: Optional[str] = None
    cr_number: Optional[str] = None
    cr_revision: Optional[str] = None
    cr_category: Optional[str] = None
    is_revision_of: Optional[str] = None
    revised_to: Optional[str] = None
    ls_to: Optional[str] = None
    ls_cc: Optional[str] = None
    file_url: Optional[str] = None
    local_zip_path: Optional[str] = None
    text_path: Optional[str] = None
    extraction_status: Optional[str] = None
    rendered_path: Optional[str] = None
    render_status: Optional[str] = None
    diff_rendered_path: Optional[str] = None
    diff_render_status: Optional[str] = None
    text: Optional[str] = None  # loaded from text_path on demand, not stored in DB


@dataclass
class MeetingSummary:
    id: int
    tsg: str
    wg_short: str
    meeting_folder: str
    modified_at: Optional[str]
    tdoc_count: int
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    location: Optional[str] = None
    country: Optional[str] = None
    display_title: Optional[str] = None


@dataclass
class Page:
    items: list
    total: int
    offset: int
    limit: int
