"""Rebuildable image features and site-scoped similarity search.

The content-addressed image store is canonical evidence.  Feature vectors and
FAISS files are disposable projections and can always be rebuilt by a worker.
"""

from __future__ import annotations

import io
import json
import math
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from .features import require_feature
from .field_memory import FieldMemoryService, MAX_STORED_IMAGE_BYTES
from .spatial_search import SpatialSearchService, _limit, _site_id


MODEL_ID = "baseline-color-spatial-v1"
VECTOR_DIMENSIONS = 160
INDEX_SCHEMA_VERSION = "1.0.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def image_vector(source: bytes | Path) -> np.ndarray:
    """Create a deterministic normalized baseline descriptor.

    This deliberately small descriptor is the offline baseline, not an AI
    model.  A reviewed model can replace it under a new model_id without
    changing stored evidence or the public query contract.
    """

    opened = io.BytesIO(source) if isinstance(source, bytes) else source
    try:
        with Image.open(opened) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
            rgb = np.asarray(image, dtype=np.float32) / 255.0
            histograms = [np.histogram(rgb[:, :, channel], bins=32, range=(0.0, 1.0))[0] for channel in range(3)]
            histogram = np.concatenate(histograms).astype(np.float32)
            histogram /= max(float(rgb.shape[0] * rgb.shape[1]), 1.0)
            luminance = np.asarray(image.convert("L").resize((8, 8), Image.Resampling.BILINEAR), dtype=np.float32).reshape(-1) / 255.0
    except (OSError, ValueError) as error:
        raise ValueError("image_feature_input_invalid") from error
    vector = np.concatenate((histogram, luminance)).astype(np.float32)
    if vector.shape != (VECTOR_DIMENSIONS,) or not np.isfinite(vector).all():
        raise ValueError("image_feature_vector_invalid")
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError("image_feature_vector_zero")
    return vector / norm


class ImageFeatureIndexService:
    """Queue rebuild jobs and query the latest site-scoped image projection."""

    def __init__(self, field_memory: FieldMemoryService) -> None:
        self.field_memory = field_memory
        self.index_root = field_memory.repository_layout.site_record_indexes / "image_features"

    def enqueue_rebuild(self, payload: dict[str, Any]) -> dict[str, Any]:
        site_id = _site_id(payload.get("site_id"))
        self.field_memory.site(site_id)
        model_id = payload.get("model_id", MODEL_ID)
        if model_id != MODEL_ID:
            raise ValueError(f"image_feature_model_not_registered:{model_id}")
        job_id = f"image-index-{uuid.uuid4().hex}"
        created_at = _now()
        with self.field_memory._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT job_id FROM image_index_jobs WHERE site_id = ? AND model_id = ? AND status IN ('queued', 'running')",
                (site_id, model_id),
            ).fetchone()
            if existing is not None:
                raise FileExistsError(f"image_feature_rebuild_already_active:{existing['job_id']}")
            connection.execute(
                "INSERT INTO image_index_jobs(job_id, site_id, model_id, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
                (job_id, site_id, model_id, created_at),
            )
        return {"job_id": job_id, "site_id": site_id, "model_id": model_id, "status": "queued", "created_at": created_at}

    def jobs(self, site_id: str) -> list[dict[str, Any]]:
        checked_site = _site_id(site_id)
        self.field_memory.site(checked_site)
        with self.field_memory.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM image_index_jobs WHERE site_id = ? ORDER BY created_at DESC LIMIT 50",
                (checked_site,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_next(self) -> dict[str, Any] | None:
        with self.field_memory._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM image_index_jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            started_at = _now()
            updated = connection.execute(
                "UPDATE image_index_jobs SET status = 'running', started_at = ? WHERE job_id = ? AND status = 'queued'",
                (started_at, row["job_id"]),
            ).rowcount
            if updated != 1:
                return None
            result = dict(row)
            result.update(status="running", started_at=started_at)
            return result

    def complete(self, job_id: str, output_count: int) -> None:
        with self.field_memory._connection(write=True) as connection:
            connection.execute(
                "UPDATE image_index_jobs SET status = 'completed', output_count = ?, completed_at = ?, failure_reason = NULL "
                "WHERE job_id = ? AND status = 'running'",
                (output_count, _now(), job_id),
            )

    def fail(self, job_id: str, error: BaseException) -> None:
        reason = f"{type(error).__name__}:{error}"[:1000]
        with self.field_memory._connection(write=True) as connection:
            connection.execute(
                "UPDATE image_index_jobs SET status = 'failed', completed_at = ?, failure_reason = ? "
                "WHERE job_id = ? AND status = 'running'",
                (_now(), reason, job_id),
            )

    def rebuild(self, site_id: str, *, require_faiss: bool = True) -> int:
        checked_site = _site_id(site_id)
        self.field_memory.site(checked_site)
        with self.field_memory.read_connection() as connection:
            documents = [
                dict(row)
                for row in connection.execute(
                    "SELECT documents.* FROM asset_documents AS documents JOIN assets USING(asset_id) "
                    "WHERE assets.site_id = ? AND documents.kind = 'image' AND documents.content_sha256 IS NOT NULL "
                    "ORDER BY documents.document_id",
                    (checked_site,),
                )
            ]

        vectors: list[np.ndarray] = []
        indexed_documents: list[dict[str, Any]] = []
        for document in documents:
            path = self.field_memory.resolve_document_content(document)
            vectors.append(image_vector(path))
            indexed_documents.append(document)
        matrix = np.stack(vectors).astype(np.float32) if vectors else np.empty((0, VECTOR_DIMENSIONS), dtype=np.float32)
        backend = self._write_index(checked_site, matrix, require_faiss=require_faiss)
        indexed_at = _now()
        with self.field_memory._connection(write=True) as connection:
            connection.execute("DELETE FROM image_features WHERE site_id = ? AND model_id = ?", (checked_site, MODEL_ID))
            connection.executemany(
                "INSERT INTO image_features(document_id, site_id, content_sha256, model_id, dimensions, vector, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        document["document_id"], checked_site, document["content_sha256"], MODEL_ID,
                        VECTOR_DIMENSIONS, sqlite3.Binary(vector.astype("<f4").tobytes()), indexed_at,
                    )
                    for document, vector in zip(indexed_documents, vectors, strict=True)
                ],
            )
        self._write_manifest(checked_site, indexed_documents, backend, indexed_at)
        return len(indexed_documents)

    def query(self, site_id: str, image_bytes: bytes, *, limit: int = 20) -> dict[str, Any]:
        checked_site = _site_id(site_id)
        checked_limit = _limit(limit, default=20)
        if not image_bytes or len(image_bytes) > MAX_STORED_IMAGE_BYTES:
            raise ValueError("image_feature_query_size_invalid")
        query_vector = image_vector(image_bytes)
        manifest = self._load_manifest(checked_site)
        document_ids = [str(item["document_id"]) for item in manifest["documents"]]
        scores_and_offsets = self._search_index(checked_site, query_vector, checked_limit, manifest)
        with self.field_memory.read_connection() as connection:
            indexed_rows = {
                str(row["document_id"]): row
                for row in connection.execute(
                    "SELECT * FROM image_features WHERE site_id = ? AND model_id = ?",
                    (checked_site, MODEL_ID),
                )
            }
            if len(indexed_rows) != len(document_ids):
                raise RuntimeError(f"image_feature_index_projection_mismatch:{checked_site}")
            results: list[dict[str, Any]] = []
            for score, offset in scores_and_offsets:
                if offset < 0 or offset >= len(document_ids):
                    continue
                document_id = document_ids[offset]
                feature = indexed_rows.get(document_id)
                manifest_entry = manifest["documents"][offset]
                if feature is None or feature["content_sha256"] != manifest_entry["content_sha256"]:
                    raise RuntimeError(f"image_feature_index_projection_mismatch:{document_id}")
                results.append(self._hydrate_result(connection, document_id, score))
        return {"site_id": checked_site, "model_id": MODEL_ID, "count": len(results), "results": results}

    def _load_manifest(self, site_id: str) -> dict[str, Any]:
        path = self.index_root / site_id / MODEL_ID / "index_manifest.json"
        if not path.is_file():
            raise FileNotFoundError(f"image_feature_index_not_built:{site_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"image_feature_index_manifest_invalid:{site_id}") from error
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != INDEX_SCHEMA_VERSION
            or payload.get("site_id") != site_id
            or payload.get("model_id") != MODEL_ID
            or payload.get("dimensions") != VECTOR_DIMENSIONS
            or not isinstance(payload.get("documents"), list)
        ):
            raise RuntimeError(f"image_feature_index_manifest_invalid:{site_id}")
        for item in payload["documents"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("document_id"), str)
                or not isinstance(item.get("content_sha256"), str)
            ):
                raise RuntimeError(f"image_feature_index_manifest_invalid:{site_id}")
        return payload

    def _search_index(
        self,
        site_id: str,
        query_vector: np.ndarray,
        limit: int,
        manifest: dict[str, Any],
    ) -> list[tuple[float, int]]:
        directory = self.index_root / site_id / MODEL_ID
        count = len(manifest["documents"])
        requested = min(limit, count)
        if requested == 0:
            return []
        backend = manifest.get("backend")
        if backend == "faiss-index-flat-ip":
            try:
                import faiss  # type: ignore[import-not-found]
            except ImportError as error:
                raise RuntimeError("image_feature_faiss_unavailable") from error
            try:
                index = faiss.read_index(str(directory / "index.faiss"))
            except RuntimeError as error:
                raise RuntimeError(f"image_feature_faiss_index_invalid:{site_id}") from error
            if index.d != VECTOR_DIMENSIONS or index.ntotal != count:
                raise RuntimeError(f"image_feature_index_projection_mismatch:{site_id}")
            scores, offsets = index.search(query_vector.reshape(1, -1), requested)
            return [(float(score), int(offset)) for score, offset in zip(scores[0], offsets[0], strict=True)]
        if backend == "numpy-test-fallback":
            try:
                with np.load(directory / "vectors.npz") as values:
                    vectors = np.asarray(values["vectors"], dtype=np.float32)
            except (OSError, KeyError, ValueError) as error:
                raise RuntimeError(f"image_feature_numpy_index_invalid:{site_id}") from error
            if vectors.shape != (count, VECTOR_DIMENSIONS) or not np.isfinite(vectors).all():
                raise RuntimeError(f"image_feature_index_projection_mismatch:{site_id}")
            scores = vectors @ query_vector
            offsets = np.argsort(scores)[::-1][:requested]
            return [(float(scores[offset]), int(offset)) for offset in offsets]
        raise RuntimeError(f"image_feature_index_backend_invalid:{backend}")

    def _write_index(self, site_id: str, matrix: np.ndarray, *, require_faiss: bool) -> str:
        directory = self.index_root / site_id / MODEL_ID
        directory.mkdir(parents=True, exist_ok=True)
        try:
            import faiss  # type: ignore[import-not-found]
        except ImportError as error:
            if require_faiss:
                raise RuntimeError("image_feature_faiss_unavailable") from error
            temporary = directory / f"vectors-{uuid.uuid4().hex}.npz"
            np.savez(temporary, vectors=matrix)
            os.replace(temporary, directory / "vectors.npz")
            return "numpy-test-fallback"
        index = faiss.IndexFlatIP(VECTOR_DIMENSIONS)
        index.add(matrix)
        temporary = directory / f"index-{uuid.uuid4().hex}.faiss"
        faiss.write_index(index, str(temporary))
        os.replace(temporary, directory / "index.faiss")
        return "faiss-index-flat-ip"

    def _write_manifest(self, site_id: str, documents: list[dict[str, Any]], backend: str, indexed_at: str) -> None:
        directory = self.index_root / site_id / MODEL_ID
        payload = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "kind": "spatial_image_feature_index",
            "site_id": site_id,
            "model_id": MODEL_ID,
            "dimensions": VECTOR_DIMENSIONS,
            "metric": "cosine",
            "backend": backend,
            "indexed_at": indexed_at,
            "documents": [
                {"document_id": item["document_id"], "content_sha256": item["content_sha256"]}
                for item in documents
            ],
        }
        temporary = directory / f"manifest-{uuid.uuid4().hex}.json"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, directory / "index_manifest.json")

    @staticmethod
    def _hydrate_result(connection: sqlite3.Connection, document_id: str, score: float) -> dict[str, Any]:
        row = connection.execute(
            "SELECT documents.*, assets.site_id, assets.name, assets.asset_type, assets.status "
            "FROM asset_documents AS documents JOIN assets USING(asset_id) WHERE documents.document_id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"image_feature_document_missing:{document_id}")
        result: dict[str, Any] = {
            "record_type": "document", "record_id": document_id, "document_id": document_id,
            "site_id": row["site_id"], "title": row["title"], "summary": row["memo"],
            "asset_id": row["asset_id"], "asset_name": row["name"], "asset_type": row["asset_type"],
            "asset_status": row["status"], "score": score, "timestamp": row["created_at"],
        }
        observation = connection.execute(
            "SELECT observations.*, scene_versions.scene_package_id FROM asset_observations AS observations "
            "JOIN scene_versions USING(scene_version_id) WHERE observations.asset_id = ? "
            "ORDER BY observations.created_at DESC LIMIT 1",
            (row["asset_id"],),
        ).fetchone()
        SpatialSearchService._add_observation_link(result, observation)
        return result


def dispatch_image_feature_api(
    service: ImageFeatureIndexService,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    *,
    enabled_features: frozenset[str],
) -> Any:
    """Dispatch the closed image-index control route family."""

    require_feature(enabled_features, "spatial-search")
    body = payload or {}
    if method == "POST" and path == "/api/spatial-search/image-index/jobs":
        return service.enqueue_rebuild(body)
    if method == "POST" and path == "/api/spatial-search/image-index/jobs/query":
        return service.jobs(_site_id(body.get("site_id")))
    raise FileNotFoundError(f"api_endpoint_not_found:{method}:{path}")
