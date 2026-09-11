# -*- coding: utf-8 -*-
"""
Shared last-run filename hand-off between the B (simulation) and C
(analysis) scripts. A B script (V3/V4/V5) calls save_filename() with
whatever `filename` it ended up using (env override or its own hardcoded
default); a C script (C1/C2/C3) calls load_filename() to pick that same
name back up, so it stops needing its own hardcoded default kept in sync
by hand. Persisted to a small file (not an env var) so this also works
across separate manual runs, not just within one excel_ochre.py process.
"""

import os

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_run_filename")


def save_filename(name):
    with open(_STATE_FILE, "w") as f:
        f.write(name)


def load_filename(default):
    try:
        with open(_STATE_FILE) as f:
            saved = f.read().strip()
        return saved or default
    except FileNotFoundError:
        return default
