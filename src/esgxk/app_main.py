"""ESG-XK 入口：GUI（默认）或 Processing Worker（--worker）。"""
from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--profile-dir" in argv and "--worker" not in argv:
        i = argv.index("--profile-dir")
        if i + 1 < len(argv):
            os.environ["ESGXK_PROFILE_DIR"] = argv[i + 1]
            del argv[i:i + 2]
    if "--worker" in argv:
        from esgxk.processing.worker import worker_main
        return worker_main(argv)
    if "--version" in argv:
        from esgxk import APP_VERSION
        from esgxk.db.migrations import SCHEMA_VERSION
        print(f"ESG-XK {APP_VERSION} (schema {SCHEMA_VERSION})")
        return 0
    from esgxk.ui.app import run_gui
    return run_gui(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
