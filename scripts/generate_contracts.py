#!/usr/bin/env python3
"""Generate raw Pydantic v2 contract shapes from Hub JSON Schema sources."""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "hub" / "contracts" / "schemas"
OUTPUT = ROOT / "hub" / "contracts" / "generated" / "schema_models"


def _generate(target: Path) -> None:
    environment = os.environ.copy()
    environment["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "datamodel_code_generator",
            "--input",
            str(SCHEMAS),
            "--input-file-type",
            "jsonschema",
            "--output",
            str(target),
            "--output-model-type",
            "pydantic_v2.BaseModel",
            "--target-python-version",
            "3.11",
            "--use-union-operator",
            "--use-annotated",
            "--enum-field-as-literal",
            "all",
            "--extra-fields",
            "forbid",
            "--enable-faux-immutability",
            "--disable-timestamp",
            "--use-default",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    ruff = str(Path(sys.executable).parent / "ruff")
    subprocess.run(
        [ruff, "check", "--fix", str(target)],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    subprocess.run(
        [ruff, "format", str(target)],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    (target / "__init__.py").write_text(
        '"""Generated schema contract shapes; do not edit manually."""\n',
        encoding="utf-8",
    )


def _same_tree(left: Path, right: Path) -> bool:
    comparison = filecmp.dircmp(left, right)
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        return False
    if any(
        not filecmp.cmp(left / name, right / name, shallow=False)
        for name in comparison.common_files
    ):
        return False
    return all(_same_tree(left / name, right / name) for name in comparison.common_dirs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="fail if committed generated files are stale"
    )
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="eidolon-contracts-") as temporary:
        candidate = Path(temporary) / "schema_models"
        _generate(candidate)
        if arguments.check:
            if not OUTPUT.is_dir() or not _same_tree(candidate, OUTPUT):
                print("generated contracts are stale; run scripts/generate_contracts.py")
                return 1
            return 0
        if OUTPUT.exists():
            shutil.rmtree(OUTPUT)
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(candidate), str(OUTPUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
