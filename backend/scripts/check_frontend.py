"""Syntax-check the inline JavaScript in the frontend pages.

There is no build step (deliberately -- the laptop only needs Python), which
means a stray quote in a page's <script> block would otherwise only show up as
a blank screen in front of the owner. This catches it in a second.

Needs node on PATH; skips with a warning if node is missing.

    python backend/scripts/check_frontend.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
SCRIPT_BLOCK = re.compile(r"<script>(.*?)</script>", re.S)


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("node is not installed; skipping the JavaScript syntax check.")
        return 0

    failures = 0
    targets = sorted(STATIC.glob("*.html")) + sorted(STATIC.glob("*.js"))

    for path in targets:
        source = path.read_text(encoding="utf-8")
        blocks = (
            SCRIPT_BLOCK.findall(source) if path.suffix == ".html" else [source]
        )
        if not blocks:
            continue

        for index, block in enumerate(blocks):
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(block)
                temp = handle.name

            result = subprocess.run(
                [node, "--check", temp], capture_output=True, text=True
            )
            Path(temp).unlink(missing_ok=True)

            label = f"{path.name}" + (f" (block {index + 1})" if len(blocks) > 1 else "")
            if result.returncode == 0:
                print(f"ok    {label}")
            else:
                failures += 1
                print(f"FAIL  {label}")
                print(result.stderr.strip()[:800])

    if failures:
        print(f"\n{failures} file(s) have JavaScript syntax errors.")
        return 1
    print("\nAll frontend scripts parse cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
