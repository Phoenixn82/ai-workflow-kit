"""Lane config: which engines serve each job shape, locked from the Phase A bake-off."""
import json
from pathlib import Path

LANES = json.loads((Path(__file__).parent / "lanes.json").read_text())
