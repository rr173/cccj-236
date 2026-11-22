#!/usr/bin/env python3
"""测试入口：以隔离模式运行（python -I run_tests.py）。

``-I`` 不会把当前目录自动放入 sys.path，这里显式插入仓库根目录，
避免依赖 PYTHONPATH（-E 下环境变量也会被忽略）。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
