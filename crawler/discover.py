import logging

from . import config
from .listing import parse_directory_listing, Entry

log = logging.getLogger("crawler.discover")


def wg_url(tsg: str, wg_path: str) -> str:
    return f"{config.BASE_URL}/tsg_{tsg}/{wg_path}"


def list_meetings(client, tsg: str, wg_path: str) -> list[Entry]:
    url = wg_url(tsg, wg_path) + "/"
    resp = client.get(url)
    entries = [e for e in parse_directory_listing(resp.text) if e.is_dir]
    log.info("Found %d meeting folders under %s", len(entries), url)
    return entries


def list_folder(client, folder_url: str) -> list[Entry]:
    if not folder_url.endswith("/"):
        folder_url += "/"
    resp = client.get(folder_url)
    return parse_directory_listing(resp.text)


def list_docs(client, meeting_url: str) -> list[Entry]:
    """List TDoc zip files in a meeting's Docs/ folder. Returns [] if the
    meeting has no Docs/ folder yet (e.g. an upcoming meeting)."""
    entries = list_folder(client, meeting_url)
    docs_dir = next((e for e in entries if e.is_dir and e.name == "Docs"), None)
    if docs_dir is None:
        return []
    return [e for e in list_folder(client, docs_dir.url) if not e.is_dir]


def list_tdoclists(client, meeting_url: str) -> list[Entry]:
    entries = list_folder(client, meeting_url)
    tdoclists_dir = next((e for e in entries if e.is_dir and e.name == "Tdoclists"), None)
    if tdoclists_dir is None:
        return []
    return list_folder(client, tdoclists_dir.url)
