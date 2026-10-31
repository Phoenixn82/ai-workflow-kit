import os
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
CONFIG = ROOT / "config"


def load(name):
    with open(CONFIG / name) as f:
        return yaml.safe_load(f) or {}


def home():
    p = Path(os.environ.get("LANES_HOME", "~/.local/share/lanes")).expanduser()
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p
