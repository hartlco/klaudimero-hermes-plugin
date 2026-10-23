"""Hermes native plugin entry point; registration performs no network I/O."""
from pathlib import Path
import sys

root = str(Path(__file__).resolve().parent)
if root not in sys.path:
    sys.path.insert(0, root)
from klaudimero_plugin.hooks import register

