#!/usr/bin/env python3
"""Verify the immutable parts of the App A generation runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_banner(command: str) -> str:
    completed = subprocess.run(
        [command, "-version" if command == "ffmpeg" else "-h"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return completed.stdout.splitlines()[0] if completed.stdout else ""


def compatible_cuda_architecture(
    capability: tuple[int, int],
    compiled_arches: list[str],
) -> str | None:
    """Return the best compatible cubin/PTX target reported by PyTorch.

    CUDA cubins are forward compatible within one compute-capability major
    version.  For example, an sm_86 cubin is valid on an Ada sm_89 GPU.  PTX
    targets are forward compatible with equal or newer compute capabilities.
    The runtime compute test below remains authoritative because third-party
    CUDA libraries do not necessarily expose every target through PyTorch.
    """

    device_value = capability[0] * 10 + capability[1]
    candidates: list[tuple[int, int, str]] = []
    for arch in compiled_arches:
        prefix, separator, digits = arch.partition("_")
        if not separator or not digits.isdigit():
            continue
        target_value = int(digits)
        target_major, target_minor = divmod(target_value, 10)
        if prefix == "sm" and target_major == capability[0] and target_minor <= capability[1]:
            candidates.append((2, target_value, arch))
        elif prefix == "compute" and target_value <= device_value:
            candidates.append((1, target_value, arch))
    return max(candidates, default=None)[2] if candidates else None


def verify(lock: dict[str, Any], model_root: Path, require_gpu: bool) -> list[str]:
    failures: list[str] = []
    expected_python = str(lock["python_major_minor"])
    actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual_python != expected_python:
        failures.append(f"python: expected {expected_python}, got {actual_python}")

    for distribution, expected in dict(lock["python_packages"]).items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            failures.append(f"python_packages.{distribution}: not installed")
            continue
        if actual != expected:
            failures.append(f"python_packages.{distribution}: expected {expected}, got {actual}")

    for command, expected_prefix in dict(lock["commands"]).items():
        try:
            actual = command_banner(command)
        except FileNotFoundError:
            failures.append(f"commands.{command}: not installed")
            continue
        if not actual.startswith(str(expected_prefix)):
            failures.append(f"commands.{command}: expected prefix {expected_prefix!r}, got {actual!r}")

    for relative_path, expected_hash in dict(lock["model_assets"]).items():
        path = model_root / relative_path
        if not path.is_file():
            failures.append(f"model_assets.{relative_path}: missing at {path}")
            continue
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            failures.append(f"model_assets.{relative_path}: expected sha256 {expected_hash}, got {actual_hash}")

    try:
        import torch
    except ImportError:
        failures.append("cuda: torch is not importable")
    else:
        expected_cuda = str(lock["cuda"]["torch_build"])
        if torch.version.cuda != expected_cuda:
            failures.append(f"cuda.torch_build: expected {expected_cuda}, got {torch.version.cuda}")
        if require_gpu and not torch.cuda.is_available():
            failures.append("cuda.runtime: torch.cuda.is_available() is false")
        elif require_gpu:
            try:
                capability = torch.cuda.get_device_capability(0)
                required_arch = f"sm_{capability[0]}{capability[1]}"
                compiled_arches = list(torch.cuda.get_arch_list())
                compatible_arch = compatible_cuda_architecture(capability, compiled_arches)
                # nvidia-smi, is_available(), and a textual architecture list do
                # not prove that the kernels used by the local VLM can execute.
                # Exercise BF16 allocation and matrix multiplication.  This is
                # authoritative even when the build reports only a compatible
                # lower-minor cubin such as sm_86 for an RTX 40-series sm_89 GPU.
                matrix = torch.ones((16, 16), device="cuda", dtype=torch.bfloat16)
                result = (matrix @ matrix).float().sum().item()
                torch.cuda.synchronize()
                if result != 4096.0:
                    failures.append(f"cuda.compute: unexpected BF16 result {result}")
                if compatible_arch is None:
                    # A successful real compute test takes precedence over the
                    # incomplete metadata returned by some CUDA extensions.
                    print(
                        "generation environment: warning: successful CUDA compute "
                        f"on {required_arch}, although PyTorch reports {compiled_arches}",
                        file=sys.stderr,
                    )
            except Exception as error:  # noqa: BLE001 - retain the CUDA reason verbatim.
                if "compatible_arch" in locals() and compatible_arch is None:
                    failures.append(
                        "cuda.architecture: GPU requires "
                        f"{required_arch}, but this PyTorch build provides {compiled_arches}"
                    )
                failures.append(f"cuda.compute: {type(error).__name__}: {error}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=Path("generation_pipeline/runtime_environment.lock.json"))
    parser.add_argument("--model-root", type=Path, default=Path("/opt/models"))
    parser.add_argument("--require-gpu", action="store_true")
    args = parser.parse_args()
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    failures = verify(lock, args.model_root, args.require_gpu)
    if failures:
        print("generation environment: FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("generation environment: VERIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
