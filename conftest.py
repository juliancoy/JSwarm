"""Resolve shared workspace modules when tests run from this repository."""
import sys
from pathlib import Path
workspace = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(workspace), str(workspace / "scripts"), str(workspace / "tests")]
