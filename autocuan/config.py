from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"


@dataclass
class Streamer:
    id: str
    name: str
    platform: str
    url: str
    language: str = "auto"      # "auto" = detected by Whisper per clip
    credit: str = ""
    hashtags: list[str] = field(default_factory=list)
    crop_center_x: float = 0.5
    facecam: dict | None = None


@dataclass
class Config:
    raw: dict[str, Any]
    data_dir: Path

    def __getitem__(self, k):
        return self.raw[k]

    @property
    def stream_entries(self) -> list:
        return self.raw.get("streams") or []


def load(path: str | Path = ROOT / "config.yaml") -> Config:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    raw = yaml.safe_load(Path(path).read_text())
    data_dir = Path(raw.get("data_dir", "./data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(raw=raw, data_dir=data_dir)


def env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None
