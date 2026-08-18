"""Load / dump recipes as YAML or JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from scrapy_awesome.recipe.models import Recipe


def load_recipe(path: str | Path) -> Recipe:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{p}: recipe file must contain a mapping")
    return Recipe.model_validate(data)


def loads_recipe(text: str) -> Recipe:
    text = text.strip()
    data = json.loads(text) if text.startswith("{") else yaml.safe_load(text)
    return Recipe.model_validate(data)


def dump_recipe(recipe: Recipe, *, fmt: str = "yaml") -> str:
    data: dict[str, Any] = recipe.to_dict()
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def save_recipe(recipe: Recipe, path: str | Path) -> Path:
    p = Path(path)
    fmt = "json" if p.suffix.lower() == ".json" else "yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_recipe(recipe, fmt=fmt), encoding="utf-8")
    return p
