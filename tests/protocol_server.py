"""Test-only entrypoint: keep disposable business projects separate from test service root."""
from pathlib import Path
import sys

from server.main import build_server

if __name__ == "__main__":
    build_server(Path(sys.argv[1])).run(transport="stdio")
