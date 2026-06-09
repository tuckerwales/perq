"""Copy text to the system clipboard via native tools."""

from __future__ import annotations

import shutil
import subprocess
import sys

if sys.platform == "darwin":
    _COMMANDS = [["pbcopy"]]
else:
    _COMMANDS = [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]


def copy_to_system_clipboard(text: str) -> bool:
    """Copy text using a native clipboard tool. Returns True on success."""
    for command in _COMMANDS:
        if shutil.which(command[0]):
            try:
                subprocess.run(command, input=text.encode(), check=True, timeout=5)
                return True
            except (subprocess.SubprocessError, OSError):
                continue
    return False
