from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    """All paths/knobs. Override via env TDOCSTORE_* or constructor."""
    data_dir: Path = field(default_factory=lambda: Path(_env("TDOCSTORE_DATA", "./data")).expanduser())
    # One SQLite DB per working group: data/db/RAN2.sqlite
    # Raw files: data/files/RAN2/R2-135/R2-2604936.zip and extracted docx alongside
    base_url: str = field(default_factory=lambda: _env("TDOCSTORE_BASE_URL", "https://www.3gpp.org/ftp"))
    user_agent: str = field(default_factory=lambda: _env("TDOCSTORE_UA", "tdocstore/0.1 (+self-hosted 3GPP TDoc index)"))
    concurrency: int = field(default_factory=lambda: int(_env("TDOCSTORE_CONCURRENCY", "3")))
    min_delay_s: float = field(default_factory=lambda: float(_env("TDOCSTORE_MIN_DELAY", "0.3")))
    max_text_chars: int = 500_000

    @property
    def db_dir(self) -> Path:
        return self.data_dir / "db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    def db_path(self, group: str) -> Path:
        self.db_dir.mkdir(parents=True, exist_ok=True)
        return self.db_dir / f"{group.upper()}.sqlite"


# 3GPP FTP layout per working group. Meeting folder pattern uses the numeric/bis suffix,
# e.g. TSGR2_135, TSGR2_133bis, TSGS2_176, TSGC1_162. Verify for new groups before use.
GROUP_LAYOUT = {
    "RAN1": {"ftp": "tsg_ran/WG1_RL1", "folder": "TSGR1_{n}", "prefix": "R1"},
    "RAN2": {"ftp": "tsg_ran/WG2_RL2", "folder": "TSGR2_{n}", "prefix": "R2"},
    "RAN3": {"ftp": "tsg_ran/WG3_Iu",  "folder": "TSGR3_{n}", "prefix": "R3"},
    "RAN4": {"ftp": "tsg_ran/WG4_Radio", "folder": "TSGR4_{n}", "prefix": "R4"},
    "RAN5": {"ftp": "tsg_ran/WG5_Test_ex-T1", "folder": "TSGR5_{n}", "prefix": "R5"},
    "RAN":  {"ftp": "tsg_ran/TSG_RAN", "folder": "TSGR_{n}", "prefix": "RP"},
    "SA1":  {"ftp": "tsg_sa/WG1_Serv", "folder": "TSGS1_{n}", "prefix": "S1"},
    "SA2":  {"ftp": "tsg_sa/WG2_Arch", "folder": "TSGS2_{n}", "prefix": "S2"},
    "SA3":  {"ftp": "tsg_sa/WG3_Security", "folder": "TSGS3_{n}", "prefix": "S3"},
    "SA4":  {"ftp": "tsg_sa/WG4_CODEC", "folder": "TSGS4_{n}", "prefix": "S4"},
    "SA5":  {"ftp": "tsg_sa/WG5_TM", "folder": "TSGS5_{n}", "prefix": "S5"},
    "SA6":  {"ftp": "tsg_sa/WG6_MissionCritical", "folder": "TSGS6_{n}", "prefix": "S6"},
    "SA":   {"ftp": "tsg_sa/TSG_SA", "folder": "TSGS_{n}", "prefix": "SP"},
    "CT1":  {"ftp": "tsg_ct/WG1_mm-cc-sm_ex-CN1", "folder": "TSGC1_{n}", "prefix": "C1"},
    "CT3":  {"ftp": "tsg_ct/WG3_interworking_ex-CN3", "folder": "TSGC3_{n}", "prefix": "C3"},
    "CT4":  {"ftp": "tsg_ct/WG4_protocollars_ex-CN4", "folder": "TSGC4_{n}", "prefix": "C4"},
    "CT6":  {"ftp": "tsg_ct/WG6_Smartcard_Ex-T3", "folder": "TSGC6_{n}", "prefix": "C6"},
    "CT":   {"ftp": "tsg_ct/TSG_CT", "folder": "TSGC_{n}", "prefix": "CP"},
}


def meeting_ref(group: str, number: str) -> str:
    """Canonical meeting_ref, TDocHamster-compatible: R2-135, R2-133-bis, S2-176."""
    p = GROUP_LAYOUT[group.upper()]["prefix"]
    n = number.lower().replace("_", "-")
    if n.endswith("bis") and not n.endswith("-bis"):
        n = n[:-3] + "-bis"
    return f"{p}-{n}"


def meeting_folder(group: str, number: str) -> str:
    """FTP folder name from meeting number: '135' -> TSGR2_135, '133-bis' -> TSGR2_133bis."""
    n = number.lower().replace("-bis", "bis").replace("_bis", "bis")
    return GROUP_LAYOUT[group.upper()]["folder"].format(n=n)


def split_meeting_ref(ref: str) -> tuple[str, str]:
    """'R2-133-bis' -> ('RAN2', '133-bis')."""
    p, _, rest = ref.partition("-")
    for g, lay in GROUP_LAYOUT.items():
        if lay["prefix"].lower() == p.lower():
            return g, rest
    raise ValueError(f"unknown meeting_ref prefix: {ref}")
