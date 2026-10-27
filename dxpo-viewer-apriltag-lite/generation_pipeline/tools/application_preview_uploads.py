"""Content-addressed upload storage for the local Application Preview API."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def list_uploaded_videos(root: Path, uploads_root: Path, suffixes: set[str]) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for candidate in uploads_root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in suffixes:
            continue
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        metadata = _read_json(candidate.parent / "upload.json")
        original_name = metadata.get("original_filename") if isinstance(metadata, dict) else None
        videos.append({
            "input_video": relative.as_posix(),
            "filename": original_name if isinstance(original_name, str) and original_name else candidate.name,
            "size_bytes": candidate.stat().st_size,
            "modified_at": datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc).isoformat(),
        })
    return sorted(videos, key=lambda item: str(item["modified_at"]), reverse=True)


def store_uploaded_video(
    *,
    root: Path,
    upload_content_root: Path,
    upload_staging_root: Path,
    suffixes: set[str],
    filename: str,
    stream: Any,
    content_length: int,
    require_headroom: Callable[[int, str], None],
) -> dict[str, Any]:
    safe_name = Path(filename).name
    if not filename or safe_name != filename or safe_name in {".", ".."}:
        raise ValueError("upload_filename_invalid")
    if Path(safe_name).suffix.lower() not in suffixes:
        raise ValueError("input_video_extension_invalid")
    if content_length <= 0:
        raise ValueError("upload_content_empty")
    require_headroom(content_length, "video_upload")

    staging_dir = upload_staging_root / uuid.uuid4().hex[:12]
    staging_dir.mkdir(parents=True, exist_ok=False)
    temporary = staging_dir / "video.partial"
    remaining = content_length
    digest = hashlib.sha256()
    try:
        with temporary.open("wb") as output:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("upload_body_incomplete")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        content_hash = digest.hexdigest()
        content_dir = upload_content_root / content_hash
        content_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(path for path in content_dir.glob("asset.*") if path.is_file())
        target = existing[0] if existing else content_dir / f"asset{Path(safe_name).suffix.lower()}"
        reused = target.exists()
        if reused:
            if target.stat().st_size != content_length:
                raise ValueError("upload_content_hash_size_mismatch")
        else:
            try:
                os.link(temporary, target)
            except FileExistsError as error:
                if not target.is_file() or target.stat().st_size != content_length:
                    raise ValueError("upload_content_hash_size_mismatch") from error
                reused = True
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                # Upload staging and the content-addressed repository may be
                # separate Docker mounts.  A hard link cannot cross that
                # boundary, so publish an atomic copy created on the target
                # filesystem instead.
                publish_temporary = content_dir / f".{target.name}.{uuid.uuid4().hex}.tmp"
                try:
                    shutil.copyfile(temporary, publish_temporary)
                    os.replace(publish_temporary, target)
                finally:
                    publish_temporary.unlink(missing_ok=True)
        metadata_path = content_dir / "upload.json"
        if not metadata_path.is_file():
            _atomic_json(metadata_path, {
                "schema_version": "1.0.0",
                "sha256": content_hash,
                "size_bytes": content_length,
                "original_filename": safe_name,
                "stored_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception:
        temporary.unlink(missing_ok=True)
        staging_dir.rmdir()
        raise
    temporary.unlink(missing_ok=True)
    staging_dir.rmdir()
    return {
        "status": "verified",
        "input_video": target.relative_to(root).as_posix(),
        "size_bytes": target.stat().st_size,
        "content_sha256": content_hash,
        "reused": reused,
    }
