"""Loaded by Hermes's authenticated dashboard plugin loader."""
from pathlib import Path
import sys

root = str(Path(__file__).resolve().parents[1])
if root not in sys.path:
    sys.path.insert(0, root)
from klaudimero_plugin.api import router

