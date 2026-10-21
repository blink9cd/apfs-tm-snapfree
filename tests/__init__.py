"""The grouped suites for apfs_tm_snapfree, plus the scripted host they share.

The subject modules live in ``src/`` and are imported by bare name, which is not on
``sys.path`` when ``python3 -m unittest discover`` imports this package from the project
root. Inserting it here, the one module every suite imports first, makes each suite
importable on its own, whichever runner or discovery order reaches it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
