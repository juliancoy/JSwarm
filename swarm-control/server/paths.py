"""Separate immutable application code, operator workspace, and durable state."""
import os
from pathlib import Path

WORKSPACE=Path(os.getenv('SWARM_WORKSPACE',str((Path(__file__).resolve().parents[3] if Path(__file__).resolve().parents[2].name == 'JSwarm' else Path(__file__).resolve().parents[2])))).resolve()
STATE=Path(os.getenv('SWARM_STATE_DIR',str(Path(__file__).with_name('.runtime')))).resolve()
