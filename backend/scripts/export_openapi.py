"""Regenerate the checked-in dashboard contract from actual FastAPI routes."""

import json
from pathlib import Path

from app.main import create_app


def main():
    target = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"
    target.write_text(json.dumps(create_app().openapi(), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
