#!/bin/sh
# The trail hook (src/agent_trail/hook.py). Always exits 0 and prints nothing on any failure,
# python3 missing included: a hook never breaks the session.
here=$(cd "$(dirname "$0")/.." && pwd) || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
PYTHONPATH="$here/src" python3 -m agent_trail hook --agent claude 2>/dev/null || true
exit 0
