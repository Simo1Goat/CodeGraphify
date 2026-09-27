#!/usr/bin/env python3
"""Regenerate contract/schema/*.json from the pydantic models.

Run after changing app/contract/models.py:
    .venv/bin/python scripts/gen_schema.py
"""

from __future__ import annotations

import json
from pathlib import Path

from app.contract import models

OUT_DIR = Path(__file__).resolve().parent.parent / "contract" / "schema"

EXPORTS = {
    "manifest": models.Manifest,
    "entity": models.Entity,
    "relation": models.Relation,
    "compilation-context": models.CompilationContext,
    "diagnostic": models.Diagnostic,
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, model in EXPORTS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://codegraphify/contract/{name}.schema.json"
        path = OUT_DIR / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2) + "\n")
        print(f"wrote {path.relative_to(OUT_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
