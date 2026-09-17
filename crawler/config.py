import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.environ.get("DATABASE_URL")

BASE_URL = "https://www.3gpp.org/ftp"

# tsg -> { short_name: ftp_path_segment }
GROUPS = {
    "ran": {
        "plenary": "TSG_RAN",
        "RAN1": "WG1_RL1",
        "RAN2": "WG2_RL2",
        "RAN3": "WG3_Iu",
        "RAN4": "WG4_Radio",
        "RAN5": "WG5_Test_ex-T1",
        "RANAH1": "AHG1_ITU_Coord",
    },
    "sa": {
        "plenary": "TSG_SA",
        "SA1": "WG1_Serv",
        "SA2": "WG2_Arch",
        "SA3": "WG3_Security",
        "SA4": "WG4_CODEC",
        "SA5": "WG5_TM",
        "SA6": "WG6_MissionCritical",
    },
    "ct": {
        "plenary": "TSG_CT",
        "CT1": "WG1_mm-cc-sm_ex-CN1",
        "CT3": "WG3_interworking_ex-CN3",
        "CT4": "WG4_protocollars_ex-CN4",
        "CT6": "WG6_Smartcard_Ex-T3",
    },
}

# Identify honestly. Not spoofing a browser and not impersonating a
# blocked bot name (3GPP's robots.txt blocks known AI-crawler UAs).
USER_AGENT = "tdoc-replica-crawler/0.1 (personal research project; contact: set-a-contact-in-config)"

# Minimum delay between HTTP requests, seconds. Keep this conservative.
REQUEST_DELAY_SECONDS = 0.4

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"
TEXT_DIR = DATA_DIR / "text"
RENDER_DIR = DATA_DIR / "rendered"


def _find_soffice() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/lib/libreoffice/program/soffice",
    ):
        if Path(candidate).exists():
            return candidate
    return None


# None if LibreOffice isn't installed — render.py checks this and reports
# a clear error rather than a confusing subprocess failure.
SOFFICE_PATH = _find_soffice()
# A handful of documents (confirmed cause: a Table-of-Contents field
# filtered by a custom paragraph style, e.g. TOC \t "Observation" — a
# 3GPP-template pattern) make LibreOffice's layout engine hang rather than
# just run slow; the earlier 120s cap let each one peg a CPU core for two
# full minutes on a 6-core machine, which was enough to starve the host
# IDE's UI thread into a Windows "AppHang". Lower, since a genuine
# multi-minute render is not expected for these documents anyway.
RENDER_TIMEOUT_SECONDS = 40
