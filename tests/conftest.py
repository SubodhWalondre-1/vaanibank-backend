"""
VaaniBank AI — Pytest Configuration
PSBs Hackathon 2026 | Team Vectora

Shared fixtures and configuration for the test suite.

Usage:
    cd backend
    python -m pytest tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure backend/ is on sys.path so imports resolve
backend_dir = str(Path(__file__).resolve().parent.parent)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
