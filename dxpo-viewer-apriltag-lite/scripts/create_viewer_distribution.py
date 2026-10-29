#!/usr/bin/env python3
"""Build a self-contained, read-only Docker Viewer ZIP.

The archive contains the App B source required for a production static build,
the explicitly selected canonical Scene Packages (including SPZ assets), and a
small static package index.  It deliberately excludes App A, input videos,
COLMAP workspaces, and API-backed import/delete operations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ALREADY_COMPRESSED_SUFFIXES = {".spz", ".rad", ".glb", ".gz", ".zip"}

if not __package__ and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation_pipeline.tools.validate_scene_package import validate_scene_package
from packages.distribution_contracts.release import application_runtime_contract
from packages.scene_repository import SceneRepositoryLayout


class DistributionError(RuntimeError):
    """The requested hand-off cannot be made safely."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_checksums(staging: Path) -> None:
    lines = []
    for path in sorted(staging.rglob("*")):
        if path.is_file() and path.name != "checksums.sha256":
            lines.append(f"{sha256(path)}  {path.relative_to(staging).as_posix()}")
    (staging / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DistributionError(f"invalid_json:{path}:{error}") from error
    if not isinstance(value, dict):
        raise DistributionError(f"json_object_required:{path}")
    return value


def assert_no_symlinks(directory: Path) -> None:
    for candidate in directory.rglob("*"):
        if candidate.is_symlink():
            raise DistributionError(f"scene_package_symlink_not_allowed:{candidate}")


def find_scene_package(root: Path, package_id: str) -> tuple[Path, dict[str, Any]]:
    if not PACKAGE_ID_RE.fullmatch(package_id):
        raise DistributionError(f"invalid_scene_package_id:{package_id}")
    package_root = SceneRepositoryLayout.from_root(root).scene_packages
    matches: list[tuple[Path, dict[str, Any]]] = []
    for manifest_path in sorted(package_root.rglob("scene_manifest.json")):
        manifest = read_json(manifest_path)
        if manifest.get("scene_package_id") == package_id:
            matches.append((manifest_path.parent, manifest))
    if not matches:
        raise DistributionError(f"scene_package_not_found:{package_id}")
    if len(matches) != 1:
        locations = ",".join(str(path) for path, _ in matches)
        raise DistributionError(f"scene_package_id_ambiguous:{package_id}:{locations}")
    return matches[0]


def verified_package(root: Path, package_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    package, manifest = find_scene_package(root, package_id)
    assert_no_symlinks(package)
    scene_id = manifest.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id:
        raise DistributionError(f"scene_package_scene_id_missing:{package_id}")
    validation = validate_scene_package(package, scene_id)
    if validation.get("status") != "verified":
        reasons = validation.get("failure_reasons", [])
        raise DistributionError(f"scene_package_not_verified:{package_id}:{','.join(str(item) for item in reasons)}")
    return package, manifest, validation


def primary_splat_name(manifest: dict[str, Any]) -> str:
    assets = manifest.get("assets")
    splats = assets.get("splats") if isinstance(assets, dict) else None
    if not isinstance(splats, list) or not splats or not isinstance(splats[0], dict):
        return "—"
    uri = splats[0].get("uri")
    return Path(uri).name if isinstance(uri, str) else "—"


def index_entry(manifest: dict[str, Any]) -> dict[str, Any]:
    package_id = str(manifest["scene_package_id"])
    return {
        "managed": True,
        "published": True,
        "status": "verified",
        "project_id": manifest.get("project_id"),
        "scene_package_id": package_id,
        "scene_id": manifest.get("scene_id"),
        "reconstruction_id": manifest.get("reconstruction_id"),
        "viewer_url": f"/?scene_package={package_id}",
        "spz": {"filename": primary_splat_name(manifest)},
        "capabilities": manifest.get("capabilities", {}),
    }


def copy_viewer_source(root: Path, staging: Path) -> None:
    source = root / "web_viewer" / "app"
    if not (source / "package.json").is_file() or not (source / "package-lock.json").is_file():
        raise DistributionError("viewer_package_lock_missing:run_npm_install_before_packaging")
    destination = staging / "web_viewer" / "app"
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("node_modules", "dist", ".vite", "coverage", "__pycache__"),
    )
    # Runtime packages, checked-in fixtures, and example splat assets are not
    # part of a hand-off. The explicitly selected canonical packages below are
    # the only scene assets published by it.
    shutil.rmtree(destination / "public" / "scene_package", ignore_errors=True)
    shutil.rmtree(destination / "public" / "assets", ignore_errors=True)
    (destination / "public" / "scene_package" / "runtime").mkdir(parents=True)


def copy_templates(root: Path, staging: Path) -> None:
    templates = root / "docker" / "viewer_distribution"
    if not templates.is_dir():
        raise DistributionError("distribution_docker_templates_missing")
    shutil.copytree(templates, staging / "docker" / "viewer_distribution")
    shutil.copytree(root / "docker" / "rosbridge_mock", staging / "docker" / "rosbridge_mock")
    shutil.copy2(templates / "docker-compose.yml", staging / "docker-compose.yml")


def copy_documentation(root: Path, staging: Path) -> None:
    """Include the contracts that a recipient needs for optional ROS testing."""
    documentation = staging / "docs"
    documentation.mkdir()
    for name in (
        "ros2_apriltag_camera_side_integration.md",
        "apriltag_dynamic_box_overlay.md",
    ):
        source = root / "docs" / "dynamic_overlay" / name
        if source.is_file():
            shutil.copy2(source, documentation / name)


def copy_rosbridge_test_sender(root: Path, staging: Path) -> None:
    """Include the browser-only rosbridge sender used for hand-off tests."""
    source = root / "examples" / "rosbridge"
    if not source.is_dir():
        raise DistributionError("rosbridge_test_sender_examples_missing")
    shutil.copytree(
        source,
        staging / "examples" / "rosbridge",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def copy_legal_notices(root: Path, staging: Path, *, public_release: bool) -> str:
    notice = root / "THIRD_PARTY_NOTICES.md"
    if not notice.is_file():
        raise DistributionError("third_party_notices_missing")
    shutil.copy2(notice, staging / notice.name)
    license_path = root / "LICENSE"
    if license_path.is_file() and license_path.stat().st_size > 0:
        shutil.copy2(license_path, staging / license_path.name)
        return "release_ready"
    if public_release:
        raise DistributionError("repository_license_missing:public_release_blocked")
    (staging / "INTERNAL_EVALUATION_ONLY.md").write_text(
        "# Internal evaluation only\n\n"
        "The repository-wide license has not been selected. This bundle must not be published or redistributed as a public software release.\n",
        encoding="utf-8",
    )
    return "internal_evaluation_only"


def write_setup_guide(staging: Path, package_entries: list[dict[str, Any]]) -> None:
    package_lines = "\n".join(
        f"- `{entry['scene_package_id']}` — scene `{entry['scene_id']}`, SPZ `{entry['spz']['filename']}`"
        for entry in package_entries
    )
    text = f"""# Spatial 3DGS Viewer 配布版

このZIPは**閲覧専用**です。選択済みの自己完結Scene Package（SPZ、メートル座標設定、測定用annotation、登録フレーム、マスク、COLMAP、Field Memory snapshot）を同梱し、DockerだけでViewerを起動できます。App AのCOLMAP再処理、SPZの新規登録・削除、入力動画、MRP編集は含みません。

## 同梱Scene Package

{package_lines}

## 必要条件

- Docker Desktop 4.0以上（Linux containers、Docker Compose v2）
- WebGL 2対応のChrome / Edge
- 初回ビルド時のDocker Hub・npmレジストリへのネットワーク接続
- SPZサイズ以上の空きディスク領域

## 起動（Windows / macOS / Linux）

ZIPを展開し、そのフォルダで次を実行します。

```bash
docker compose up --build -d viewer
docker compose ps
```

`viewer` が `running` になったら、<http://localhost:8080/> を開き、Scene Packageを選択します。コンテナログは次で確認できます。

```bash
docker compose logs -f viewer
```

LAN内の別PCから開く場合は、配布PCのIPアドレスを使って`http://<配布PCのIP>:8080/` を開き、Windows FirewallでTCP 8080を許可してください。

## 動的箱のDocker通信テスト（任意）

```bash
docker compose --profile dynamic-test up --build -d
```

通常Viewerを開いてからInspectorの`動的追跡`で`実験シミュレータを開く`を押します。子Viewerでは
`送信する箱`から各箱を選んで位置・姿勢を設定できます。`ROS送信を開始`は登録済みの全箱を同一
`/box/relative_transform` topicへ各20 Hzで送信し、親Viewerは`child_frame_id`ごとに独立して表示・
timeout・距離計測します。親Viewerで`接続`を押してください。`rosbridge-mock`は契約確認専用で、ROS 2、
実カメラ、AprilTag検出器の代わりにはなりません。

## 実ROS 2と接続する場合

ROS PCで`rosbridge_server`を起動し、Viewer InspectorのURLを`ws://<ROS-PC-IP>:9090`（HTTPS配信時は`wss://...`）へ変更します。topic、message、座標・品質契約は、このZIP内の[`docs/ros2_apriltag_camera_side_integration.md`](docs/ros2_apriltag_camera_side_integration.md)に従ってください。

## 停止・削除

```bash
docker compose down
```

`docker compose down`はこの配布版のコンテナだけを停止・削除します。ZIPを展開したフォルダと同梱Scene Packageは削除しません。
"""
    (staging / "SETUP.md").write_text(text, encoding="utf-8")


def create_distribution(root: Path, package_ids: list[str], output: Path, *, public_release: bool = False) -> dict[str, Any]:
    if not package_ids:
        raise DistributionError("at_least_one_scene_package_is_required")
    if len(set(package_ids)) != len(package_ids):
        raise DistributionError("duplicate_scene_package_id")

    selected = [verified_package(root, package_id) for package_id in package_ids]
    entries = [index_entry(manifest) for _, manifest, _ in selected]
    bundle_name = f"spatial-3dgs-viewer-{package_ids[0]}"

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
    partial.unlink(missing_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="spatial-viewer-distribution-") as temporary:
            staging = Path(temporary) / bundle_name
            staging.mkdir()
            copy_viewer_source(root, staging)
            copy_templates(root, staging)
            copy_documentation(root, staging)
            copy_rosbridge_test_sender(root, staging)
            distribution_scope = copy_legal_notices(root, staging, public_release=public_release)
            package_destination = staging / "scene_packages"
            package_destination.mkdir()
            for package, manifest, validation in selected:
                destination = package_destination / str(manifest["scene_package_id"])
                shutil.copytree(package, destination)
                (destination / "distribution_validation.json").write_text(
                    json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            index = {
                "schema_version": "1.0.0",
                "kind": "spatial_3dgs_viewer_distribution_index",
                "packages": entries,
            }
            index_path = staging / "web_viewer" / "app" / "public" / "scene_package" / "distribution_index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            bundle_manifest = {
                "schema_version": "1.0.0",
                "kind": "spatial_3dgs_viewer_distribution",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "distribution_scope": distribution_scope,
                "packages": [
                    {
                        "scene_package_id": entry["scene_package_id"],
                        "scene_manifest_sha256": sha256(package / "scene_manifest.json"),
                    }
                    for (package, _, _), entry in zip(selected, entries, strict=True)
                ],
            }
            (staging / "bundle_manifest.json").write_text(
                json.dumps(bundle_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (staging / "app_package_manifest.json").write_text(
                json.dumps({
                    "schema_version": "1.0.0",
                    "kind": "spatial_3dgs_application_distribution",
                    "application_id": "viewer",
                    "application_version": application_runtime_contract()["version"],
                    "created_at": bundle_manifest["created_at"],
                    "distribution_scope": distribution_scope,
                    "entrypoint": "http://localhost:8080/",
                    "api_feature_allowlist": [],
                    "requirements": {"gpu": False, "camera": False, "rosbridge": True},
                    "data_access": {"read": ["bundled verified Scene Packages"], "write": []},
                    "canonical_data_included": True,
                }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_setup_guide(staging, entries)
            write_checksums(staging)
            with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
                for path in sorted(staging.rglob("*")):
                    if not path.is_file():
                        continue
                    compression = zipfile.ZIP_STORED if path.suffix.lower() in ALREADY_COMPRESSED_SUFFIXES else zipfile.ZIP_DEFLATED
                    archive.write(path, path.relative_to(staging.parent).as_posix(), compress_type=compression)
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "status": "verified",
        "output": str(output),
        "size_bytes": output.stat().st_size,
        "sha256": sha256(output),
        "scene_package_ids": package_ids,
        "distribution_scope": distribution_scope,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a self-contained Docker Viewer ZIP from verified Scene Packages.")
    parser.add_argument("--scene-package", action="append", dest="scene_packages", required=True, help="Verified scene_package_id to include; repeat for multiple packages.")
    parser.add_argument("--output", required=True, type=Path, help="Output .zip path. This generated artifact is not committed.")
    parser.add_argument("--public-release", action="store_true", help="Require a repository LICENSE and mark the bundle release-ready.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.output.suffix.lower() != ".zip":
        raise SystemExit("--output must end in .zip")
    try:
        result = create_distribution(ROOT, list(args.scene_packages), args.output, public_release=args.public_release)
    except DistributionError as error:
        raise SystemExit(f"viewer_distribution_failed:{error}") from error
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
