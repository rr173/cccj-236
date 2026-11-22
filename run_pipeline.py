#!/usr/bin/env python3
"""离线管线入口（隔离模式友好）。

用法：
  python -I run_pipeline.py batch --definitions ... --snapshots ... --out-dir ...
  python -I run_pipeline.py one --definition ... --snapshot ...
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
