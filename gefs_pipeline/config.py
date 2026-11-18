"""Config loading.  config.toml -> nested SimpleNamespace (dotted access)."""
from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"


def _ns(x):
    if isinstance(x, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in x.items()})
    if isinstance(x, list):
        return [_ns(i) for i in x]
    return x


def load_config(path: str | Path | None = None) -> SimpleNamespace:
    with open(path or DEFAULT_CONFIG, "rb") as fh:
        return _ns(tomllib.load(fh))


def member_names(cfg) -> list[str]:
    """Control + perturbed members, e.g. ['gec00', 'gep01', ..., 'gep30']."""
    return ["gec00"] + [f"gep{i:02d}" for i in range(1, int(cfg.source.n_perturbed) + 1)]
