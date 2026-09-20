# -*- coding: utf-8 -*-
"""单元测试入口：py run_tests.py（E2E 单独跑 tests/test_e2e_demo.py）。"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
TESTS = ["tests/test_llm_chain.py", "tests/test_filler.py"]

failed = 0
for t in TESTS:
    print(f"\n===== {t} =====")
    r = subprocess.run([sys.executable, str(BASE / t)], cwd=str(BASE))
    if r.returncode != 0:
        failed += 1

print(f"\n===== 汇总：{len(TESTS) - failed}/{len(TESTS)} 个测试文件通过 =====")
sys.exit(1 if failed else 0)
