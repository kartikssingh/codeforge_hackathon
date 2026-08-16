#!/usr/bin/env python3
"""Export the triage model for NPU inference.

* macOS  → ONNX, for the Apple Neural Engine via ``onnxruntime-genai``
* others → OpenVINO IR (int4), for the Intel Core Ultra NPU via ``openvino-genai``

Run once, with an internet connection, before going offline::

    pip install "optimum[openvino,onnxruntime]" transformers
    python backend/scripts/export_npu_model.py            # ~2 GB download
    python backend/scripts/export_npu_model.py --force    # re-export

The result lands in ``backend/models/{onnx,openvino}`` and is picked up
automatically when the dashboard's NPU toggle is on.  If the export is missing
or the runtime is not installed, ARIA falls back to Ollama and then to the
deterministic rule engine — nothing breaks, it just gets less clever.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BACKEND_DIR / "models"
DEFAULT_MODEL_ID = "google/gemma-3-1b-it"


def run(command: list[str]) -> int:
    print("$", " ".join(command), flush=True)
    try:
        return subprocess.run(command, check=True).returncode
    except FileNotFoundError:
        print(f"Command not found: {command[0]}", file=sys.stderr)
        return 127
    except subprocess.CalledProcessError as exc:
        print(f"Export failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode


def require_optimum() -> bool:
    try:
        import optimum  # noqa: F401
    except ImportError:
        print(
            'optimum is required:\n  pip install "optimum[openvino,onnxruntime]" transformers',
            file=sys.stderr,
        )
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--force", action="store_true", help="Re-export over an existing directory")
    parser.add_argument(
        "--target",
        choices=("auto", "onnx", "openvino"),
        default="auto",
        help="Override the runtime chosen for this platform",
    )
    args = parser.parse_args(argv)

    if not require_optimum():
        return 1

    target = args.target
    if target == "auto":
        target = "onnx" if platform.system() == "Darwin" else "openvino"

    out_dir = MODELS_DIR / target
    if out_dir.exists():
        if not args.force:
            print(f"{out_dir} already exists — pass --force to re-export.")
            return 0
        print(f"Removing existing export at {out_dir}")
        shutil.rmtree(out_dir)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    if target == "onnx":
        command = [
            sys.executable, "-m", "optimum.exporters.onnx",
            "--model", args.model_id,
            "--task", "text-generation-with-past",
            str(out_dir),
        ]
    else:
        # int4 keeps the model inside the Core Ultra Gen 1 NPU compile budget.
        command = [
            "optimum-cli", "export", "openvino",
            "--model", args.model_id,
            "--task", "text-generation-with-past",
            "--weight-format", "int4",
            str(out_dir),
        ]

    code = run(command)
    if code == 0:
        print(f"\nExported {args.model_id} → {out_dir}")
        print("Switch the dashboard to NPU mode to use it.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
