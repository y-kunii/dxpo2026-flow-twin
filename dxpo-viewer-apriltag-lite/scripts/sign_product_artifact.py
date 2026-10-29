#!/usr/bin/env python3
"""Create or verify a detached signing statement for a Product artifact.

The private key is supplied by the release owner and is never copied into the
repository or distribution.  The signed statement binds the Runtime Bundle
and optional SBOM/material files by SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file() or path.is_symlink():
        raise ValueError(f"{label}_must_be_regular_file")
    return resolved


def _run(argv: list[str]) -> bytes:
    try:
        process = subprocess.run(argv, check=True, capture_output=True)
    except FileNotFoundError as error:
        raise ValueError("openssl_not_available") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace")[-2000:]
        raise ValueError(f"openssl_operation_failed:{detail}") from error
    return process.stdout


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError("signed_material_must_be_within_artifact_root") from error


def sign(
    artifact: Path,
    private_key: Path,
    *,
    root: Path,
    materials: list[Path],
    output_prefix: Path,
    force: bool,
) -> dict[str, str]:
    root = root.resolve()
    artifact = _regular(artifact, "artifact")
    private_key = _regular(private_key, "private_key")
    checked_materials = [_regular(path, "material") for path in materials]
    statement_path = output_prefix.with_suffix(output_prefix.suffix + ".signing-statement.json")
    signature_path = output_prefix.with_suffix(output_prefix.suffix + ".signing-statement.sig")
    public_key_path = output_prefix.with_suffix(output_prefix.suffix + ".public-key.pem")
    for path in (statement_path, signature_path, public_key_path):
        if path.exists() and not force:
            raise ValueError(f"signature_output_exists:{path.name}")
        path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spatial-signing-") as temporary:
        temporary_public = Path(temporary) / "public-key.pem"
        _run(["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(temporary_public)])
        public_der = _run([
            "openssl", "pkey", "-pubin", "-in", str(temporary_public), "-outform", "DER",
        ])
        statement: dict[str, Any] = {
            "schema_version": "1.0.0",
            "kind": "spatial_product_artifact_signing_statement",
            "algorithm": "sha256-with-openssl-pkey",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "artifact": {
                "path": _relative(artifact, root),
                "sha256": _sha256(artifact),
                "size_bytes": artifact.stat().st_size,
            },
            "materials": [
                {
                    "path": _relative(path, root),
                    "sha256": _sha256(path),
                    "size_bytes": path.stat().st_size,
                }
                for path in checked_materials
            ],
            "public_key_sha256": hashlib.sha256(public_der).hexdigest(),
        }
        partial_statement = statement_path.with_name(f".{statement_path.name}.{os.getpid()}.partial")
        partial_signature = signature_path.with_name(f".{signature_path.name}.{os.getpid()}.partial")
        partial_public = public_key_path.with_name(f".{public_key_path.name}.{os.getpid()}.partial")
        partial_statement.write_text(
            json.dumps(statement, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _run([
            "openssl", "dgst", "-sha256", "-sign", str(private_key),
            "-out", str(partial_signature), str(partial_statement),
        ])
        partial_public.write_bytes(temporary_public.read_bytes())
        os.replace(partial_statement, statement_path)
        os.replace(partial_signature, signature_path)
        os.replace(partial_public, public_key_path)
    return {
        "status": "signed",
        "statement": str(statement_path),
        "signature": str(signature_path),
        "public_key": str(public_key_path),
        "artifact_sha256": statement["artifact"]["sha256"],
        "statement_sha256": _sha256(statement_path),
    }


def verify(statement_path: Path, signature_path: Path, public_key_path: Path, *, root: Path) -> dict[str, Any]:
    statement_path = _regular(statement_path, "statement")
    signature_path = _regular(signature_path, "signature")
    public_key_path = _regular(public_key_path, "public_key")
    root = root.resolve()
    _run([
        "openssl", "dgst", "-sha256", "-verify", str(public_key_path),
        "-signature", str(signature_path), str(statement_path),
    ])
    statement = json.loads(statement_path.read_text(encoding="utf-8"))
    if statement.get("kind") != "spatial_product_artifact_signing_statement":
        raise ValueError("signing_statement_kind_invalid")
    public_der = _run([
        "openssl", "pkey", "-pubin", "-in", str(public_key_path), "-outform", "DER",
    ])
    if hashlib.sha256(public_der).hexdigest() != statement.get("public_key_sha256"):
        raise ValueError("signing_public_key_fingerprint_mismatch")
    artifact = statement.get("artifact")
    materials = statement.get("materials")
    if not isinstance(artifact, dict) or not isinstance(materials, list):
        raise ValueError("signing_statement_material_invalid")
    verified = []
    for item in [artifact, *materials]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("signing_statement_material_invalid")
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
            raise ValueError("signed_material_path_invalid")
        if _sha256(path) != item.get("sha256") or path.stat().st_size != item.get("size_bytes"):
            raise ValueError(f"signed_material_digest_mismatch:{item['path']}")
        verified.append(item["path"])
    return {
        "status": "verified",
        "statement_sha256": _sha256(statement_path),
        "public_key_sha256": statement["public_key_sha256"],
        "verified_materials": verified,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sign_parser = commands.add_parser("sign")
    sign_parser.add_argument("artifact", type=Path)
    sign_parser.add_argument("--private-key", required=True, type=Path)
    sign_parser.add_argument("--artifact-root", type=Path)
    sign_parser.add_argument("--material", action="append", default=[], type=Path)
    sign_parser.add_argument("--output-prefix", type=Path)
    sign_parser.add_argument("--force", action="store_true")
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--statement", required=True, type=Path)
    verify_parser.add_argument("--signature", required=True, type=Path)
    verify_parser.add_argument("--public-key", required=True, type=Path)
    verify_parser.add_argument("--artifact-root", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "sign":
        artifact = args.artifact.resolve()
        result = sign(
            artifact,
            args.private_key,
            root=(args.artifact_root or artifact.parent),
            materials=args.material,
            output_prefix=(args.output_prefix or artifact),
            force=args.force,
        )
    else:
        result = verify(
            args.statement,
            args.signature,
            args.public_key,
            root=args.artifact_root,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
