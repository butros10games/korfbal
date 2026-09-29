"""Provider roster observation rules shared by publication and team reads."""

from datetime import timedelta


# A provider roster observation older than this no longer proves membership.
ROSTER_FRESHNESS = timedelta(days=8)
