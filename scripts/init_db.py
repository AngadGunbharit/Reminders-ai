"""Create (or reset) the local SQLite database.

Usage:
    python -m scripts.init_db          # create if missing
    python -m scripts.init_db --reset  # wipe and recreate
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db  # noqa: E402


def main() -> None:
    reset = "--reset" in sys.argv
    db.init_db(reset=reset)
    print(f"{'Reset' if reset else 'Initialised'} {db.DB_PATH}")


if __name__ == "__main__":
    main()
