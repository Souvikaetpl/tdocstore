from dataclasses import dataclass
from typing import Optional


@dataclass
class User:
    id: int
    email: str
    display_name: Optional[str]
    is_admin: bool
    status: str
    mcp_access: bool
