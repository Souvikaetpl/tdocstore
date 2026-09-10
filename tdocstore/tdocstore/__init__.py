"""tdocstore — self-hosted 3GPP TDoc data layer.

Layering (do not bypass):
  ingest.py / fetch.py / extract.py   -> write path (crawl, extract, index)
  store.py                            -> the ONLY read API; every adapter calls this
  mcp_server.py                       -> MCP adapter (stdio + streamable HTTP)
"""
from .store import Store  # noqa: F401

__version__ = "0.1.0"
