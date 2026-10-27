"""Support python -m sharebus_cli as well as the installed console command."""

import sys

from . import main

sys.exit(main())
