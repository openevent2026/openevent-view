#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT_DIR/build"
PYTHON_BIN="${PYTHON:-python3}"
PYCACHE_DIR="$BUILD_DIR/pycache"
TMP_WORK_DIR="$BUILD_DIR/tmp"

rm -rf "$PYCACHE_DIR" "$TMP_WORK_DIR"
mkdir -p "$PYCACHE_DIR" "$TMP_WORK_DIR"

export PYTHONPYCACHEPREFIX="$PYCACHE_DIR"
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export TMPDIR="$TMP_WORK_DIR"

cd "$ROOT_DIR"

"$PYTHON_BIN" - <<'PY'
import importlib.util
import importlib.metadata
import sys

requirements = [
    ("yaml", "PyYAML"),
    ("grpc", "grpcio"),
    ("openevent.sdk", "openevent-sdk>=0.4.0"),
]
missing = [package for module, package in requirements if importlib.util.find_spec(module) is None]
if missing:
    print("missing Python dependencies in the current environment:", ", ".join(missing), file=sys.stderr)
    print(
        "install them from the normal package source before running tests",
        file=sys.stderr,
    )
    sys.exit(2)

try:
    version = importlib.metadata.version("openevent-sdk")
except importlib.metadata.PackageNotFoundError:
    print("missing Python dependency: openevent-sdk>=0.4.0", file=sys.stderr)
    sys.exit(2)
parts = tuple(int(part) for part in version.split(".")[:3] if part.isdigit())
if parts < (0, 4, 0):
    print(f"openevent-sdk>=0.4.0 is required, found {version}", file=sys.stderr)
    sys.exit(2)
PY

export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

"$PYTHON_BIN" -m unittest discover -s tests -v "$@"

printf 'test artifacts: %s\n' "$BUILD_DIR"
