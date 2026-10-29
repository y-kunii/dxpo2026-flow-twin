"""Small local API for the Application Preview milestone.

The API is deliberately a thin adapter around the existing scene runner and
validators.  It owns job bookkeeping and read-only artifact discovery, but it
does not implement COLMAP, masking, MRP export, proxy generation, or viewer
logic itself.
"""

from __future__ import annotations

if __package__:
    from .application_services import (
        DistributionService,
        PipelineJobService,
        ProjectWorkspaceService,
        RepositoryService,
        SceneImportService,
    )
    from .application_services.common import *  # noqa: F403
else:  # Keep direct imports working while the legacy HTTP entry point stays disabled.
    import sys as _sys
    from pathlib import Path as _Path

    _repository_root = _Path(__file__).resolve().parents[2]
    if str(_repository_root) not in _sys.path:
        _sys.path.insert(0, str(_repository_root))

    from generation_pipeline.tools.application_services import (
        DistributionService,
        PipelineJobService,
        ProjectWorkspaceService,
        RepositoryService,
        SceneImportService,
    )
    from generation_pipeline.tools.application_services.common import *  # noqa: F403


class PreviewService(
    RepositoryService,
    DistributionService,
    ProjectWorkspaceService,
    PipelineJobService,
    SceneImportService,
):
    def __init__(self, root: Path = REPO_ROOT) -> None:
        self.root = root.resolve()
        self.repository_layout = SceneRepositoryLayout.from_root(self.root)
        self.repository_layout.ensure_runtime_roots()
        self.jobs_root = self.repository_layout.jobs
        self.projects_root = self.repository_layout.projects
        self.uploads_root = self.repository_layout.source_files
        self.upload_content_root = self.repository_layout.sources
        self.upload_staging_root = self.repository_layout.upload_staging
        self.imports_root = self.repository_layout.imports
        self.import_locks_root = self.repository_layout.locks / IMPORT_LOCKS_DIRECTORY
        self.deleted_scene_packages_root = self.repository_layout.scene_package_trash
        self.app_packages_root = self.repository_layout.application_deliverables
        self.app_package_jobs_root = self.jobs_root / "application_packages"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.upload_content_root.mkdir(parents=True, exist_ok=True)
        self.upload_staging_root.mkdir(parents=True, exist_ok=True)
        self.imports_root.mkdir(parents=True, exist_ok=True)
        self.import_locks_root.mkdir(parents=True, exist_ok=True)
        self.deleted_scene_packages_root.mkdir(parents=True, exist_ok=True)
        self.app_packages_root.mkdir(parents=True, exist_ok=True)
        configured_minimum = os.environ.get("APP_PREVIEW_MIN_FREE_BYTES", str(DEFAULT_MIN_FREE_BYTES))
        try:
            self.min_free_bytes = max(0, int(configured_minimum))
        except ValueError as error:
            raise ValueError("APP_PREVIEW_MIN_FREE_BYTES must be a non-negative integer") from error
        self.started_at = utc_now()
        self._lock = threading.Lock()
        self._package_lock = threading.Lock()
        self._app_package_jobs_lock = threading.Lock()
        self._app_package_job_threads: dict[str, threading.Thread] = {}
        self._app_package_cancel_events: dict[str, threading.Event] = {}
        self._storage_cleanup_lock = threading.Lock()
        self._repository_cleanup_lock = threading.Lock()
        self._catalog_lock = threading.Lock()
        self._repository_size_cache: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._scene_package_cache_lock = threading.RLock()
        self._scene_packages_cache_signature: tuple[Any, ...] | None = None
        self._scene_packages_cache_dependencies: tuple[Path, ...] = ()
        self._scene_packages_cache: list[dict[str, Any]] | None = None
        self._scene_package_size_cache: OrderedDict[
            str, tuple[tuple[Any, ...], int]
        ] = OrderedDict()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._live_progress_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._apriltag_live_detector = AprilTagLiveDetector()
        self._storage_lifecycle = StorageLifecycle(self.root)
        self._reconcile_interrupted_app_package_jobs()

class PreviewHandler(BaseHTTPRequestHandler):
    """HTTP adapter retained exclusively for explicit in-process tests."""

    server_version = "SpatialApplicationPreview/1.0"
    # Camera preview sends a continuous sequence of small requests. HTTP/1.1
    # keeps the localhost/WSL connection reusable and avoids reconnect stalls.
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> PreviewService:
        return self.server.preview_service  # type: ignore[attr-defined]

    def _write_response(self, status: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(json_hashable(payload), indent=2).encode("utf-8")
        self._write_response(status, "application/json; charset=utf-8", body)

    def _send_download(self, path: Path) -> None:
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _redirect_to_viewer(self) -> None:
        request_host = self.headers.get("Host", "127.0.0.1")
        if request_host.startswith("["):
            viewer_host = request_host.partition("]")[0] + "]"
        else:
            viewer_host = request_host.split(":", 1)[0]
        try:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"http://{viewer_host}:5173/?preview=pipeline")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("request_body_too_large")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request_body_must_be_object")
        return value

    def _dispatch(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        return dispatch_preview_api(
            self.service,
            method,
            path,
            payload,
            server_version=self.server_version,
            enabled_features=self.server.enabled_features,
        )

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._body() if method == "POST" else None
            result = self._dispatch(method, parsed.path, payload)
            self._send(HTTPStatus.OK, result)
        except ImmutableScenePackageError as error:
            self._send(HTTPStatus.CONFLICT, {
                "status": "failed",
                "failure_reasons": [str(error)],
                "immutable": True,
                "required_action": "create_a_new_scene_package_id_via_verified_mrp_and_spz_import",
            })
        except DeprecatedScenePackageApiError as error:
            self._send(HTTPStatus.GONE, {
                "status": "failed",
                "failure_reasons": [str(error)],
                "required_action": "use_verified_mrp_and_spz_import",
            })
        except ApplicationFeatureDisabled as error:
            self._send(HTTPStatus.FORBIDDEN, {"status": "failed", "failure_reasons": [str(error)]})
        except (FileNotFoundError, ValueError, SceneConfigError) as error:
            self._send(HTTPStatus.NOT_FOUND if isinstance(error, FileNotFoundError) else HTTPStatus.BAD_REQUEST, {"status": "failed", "failure_reasons": [str(error)]})
        except Exception as error:  # noqa: BLE001 - API must serialize failures
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "failed", "failure_reasons": [str(error)]})

    def do_GET(self) -> None:  # noqa: N802
        parsed_path = urlparse(self.path).path
        if parsed_path in {"/", "/app"}:
            self._redirect_to_viewer()
            return
        parts = [unquote(item) for item in parsed_path.split("/") if item]
        if len(parts) == 4 and parts[:2] == ["api", "app-packages"] and parts[3] == "download":
            try:
                require_feature(self.server.enabled_features, "package-builder")
                self._send_download(self.service.app_package_download(parts[2]))
            except ApplicationFeatureDisabled as error:
                self._send(HTTPStatus.FORBIDDEN, {"status": "failed", "failure_reasons": [str(error)]})
            except (FileNotFoundError, ValueError) as error:
                self._send(HTTPStatus.NOT_FOUND if isinstance(error, FileNotFoundError) else HTTPStatus.BAD_REQUEST, {
                    "status": "failed", "failure_reasons": [str(error)],
                })
            return
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [unquote(item) for item in parsed.path.split("/") if item]
        if parts not in (["api", "upload-video"], ["api", "import-spz"], ["api", "apriltag-detect"]):
            self._send(HTTPStatus.NOT_FOUND, {"status": "failed", "failure_reasons": ["api_endpoint_not_found"]})
            return
        try:
            if parts == ["api", "upload-video"]:
                require_feature(self.server.enabled_features, "pipeline")
            elif parts == ["api", "import-spz"]:
                require_feature(self.server.enabled_features, "spz-import")
            else:
                require_feature(self.server.enabled_features, "apriltag-check")
            query = parse_qs(parsed.query)
            content_length = int(self.headers.get("Content-Length", "0"))
            if parts == ["api", "apriltag-detect"]:
                if content_length <= 0 or content_length > MAX_LIVE_FRAME_BYTES:
                    raise ValueError("apriltag_live_frame_size_invalid")
                content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
                if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                    raise ValueError("apriltag_live_frame_content_type_invalid")
                required = {name: (query.get(name) or [""])[0] for name in ("family", "tag_size_m", "fx", "fy", "cx", "cy")}
                result = self.service.detect_apriltag_frame(
                    self.rfile.read(content_length),
                    family=required["family"],
                    tag_size_m=required["tag_size_m"],
                    fx=required["fx"],
                    fy=required["fy"],
                    cx=required["cx"],
                    cy=required["cy"],
                    distortion_coefficients=(query.get("distortion_coefficients") or [""])[0],
                )
            elif parts == ["api", "upload-video"]:
                filename = (query.get("filename") or [""])[0]
                result = self.service.store_uploaded_video(filename, self.rfile, content_length)
            else:
                filename = (query.get("filename") or [""])[0]
                scene_id = (query.get("scene_id") or [""])[0]
                reconstruction_id = (query.get("reconstruction_id") or [""])[0]
                result = self.service.import_spz(filename, self.rfile, content_length, scene_id, reconstruction_id)
            self._send(HTTPStatus.OK, result)
        except FileExistsError as error:
            self._send(HTTPStatus.CONFLICT, {
                "status": "failed",
                "failure_reasons": [str(error)],
                "immutable": True,
            })
        except ApplicationFeatureDisabled as error:
            self._send(HTTPStatus.FORBIDDEN, {"status": "failed", "failure_reasons": [str(error)]})
        except (ValueError, OSError) as error:
            self._send(HTTPStatus.BAD_REQUEST, {"status": "failed", "failure_reasons": [str(error)]})
        except Exception as error:  # noqa: BLE001 - API must serialize failures
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "failed", "failure_reasons": [str(error)]})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


class PreviewServer(ThreadingHTTPServer):
    """Legacy adapter that can only be enabled by an explicit test harness."""

    def __init__(
        self,
        address: tuple[str, int],
        service: PreviewService,
        enabled_features: frozenset[str] = frozenset({"suite"}),
        *,
        test_harness: bool = False,
    ) -> None:
        if not test_harness:
            raise RuntimeError(
                "legacy_preview_server_test_harness_only:"
                "use_python_-m_services.control_api"
            )
        super().__init__(address, PreviewHandler)
        self.preview_service = service
        self.enabled_features = enabled_features


def main() -> None:
    raise SystemExit(
        "The legacy PreviewServer entry point is disabled. "
        "Start the authenticated API with `python3 -m services.control_api`."
    )


if __name__ == "__main__":
    main()
