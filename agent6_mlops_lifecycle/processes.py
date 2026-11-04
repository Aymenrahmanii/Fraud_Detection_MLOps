"""Platform-specific flags for hidden, supervised child processes."""

import subprocess
import sys

if sys.platform == "win32":
    NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    NO_WINDOW_FLAGS = 0
