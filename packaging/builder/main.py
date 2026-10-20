# Invoked by /usr/bin/python3 -I; never import modules from the upload or cwd.
import sys

sys.path.insert(0, "/usr/lib/shinyd-builder")
from builder.cli import main

raise SystemExit(main())
