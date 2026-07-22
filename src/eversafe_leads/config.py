"""Config loading for jurisdictions and scoring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def load_jurisdictions(config_dir: Path = CONFIG_DIR) -> dict[str, dict[str, Any]]:
    return _load_yaml(config_dir / "jurisdictions.yaml")


def load_jurisdiction(slug: str, config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    data = load_jurisdictions(config_dir)
    if slug not in data:
        raise KeyError(f"unknown jurisdiction '{slug}'. known: {sorted(data)}")
    return data[slug]


def load_scoring(config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    return _load_yaml(config_dir / "scoring.yaml")
