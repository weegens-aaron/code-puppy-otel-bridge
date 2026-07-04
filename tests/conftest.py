"""Pytest fixtures for otel_bridge tests.

otel_bridge lives in ``~/.code_puppy/plugins/otel_bridge`` and uses
relative imports (``from . import config``). At runtime code-puppy's
plugin loader handles that via ``spec_from_file_location``; under bare
pytest we just need the plugins directory on ``sys.path`` so
``import otel_bridge`` resolves as a plain top-level package. This is
the standard conftest pattern for code-puppy user plugins.
"""

from __future__ import annotations

import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGINS_DIR = os.path.dirname(_PLUGIN_DIR)

for _path in (_PLUGINS_DIR, _PLUGIN_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
