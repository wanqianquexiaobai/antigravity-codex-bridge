#!/usr/bin/env python3
"""Open the same isolated Codex environment used by the MCP worker."""
import os
from pathlib import Path
import sys
from bridge import Bridge

bridge = Bridge(Path(__file__).with_name("settings.json"))
os.execve(str(bridge.binary), [str(bridge.binary), *sys.argv[1:]], bridge.environment())
