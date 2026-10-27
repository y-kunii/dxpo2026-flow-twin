"""Append-oriented field memory registry backed by a dedicated SQLite store.

This feature records business metadata that refers to immutable Scene Packages.
It never edits MRP, SPZ, Scene Package, or Viewer runtime artifacts.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from PIL import Image

from packages.scene_package_integrity import scene_package_reference_lock
from packages.scene_repository import SceneRepositoryLayout

from .features import require_feature


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
QUATERNION_TOLERANCE = 1e-6
SCHEMA_VERSION = 12
MINIMUM_MIGRATABLE_SCHEMA_VERSION = 1
MAX_STORED_IMAGE_BYTES = 20 * 1024 * 1024
MAX_STORED_DOCUMENT_BYTES = 100 * 1024 * 1024
STORED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_versions (
    scene_version_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    scene_package_id TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    captured_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS captures (
    capture_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    scene_version_id TEXT UNIQUE REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    source_type TEXT NOT NULL,
    source_content_sha256 TEXT,
    captured_at TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    status TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_observations (
    observation_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
    scene_version_id TEXT NOT NULL REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    position_x REAL NOT NULL,
    position_y REAL NOT NULL,
    position_z REAL NOT NULL,
    rotation_x REAL NOT NULL,
    rotation_y REAL NOT NULL,
    rotation_z REAL NOT NULL,
    rotation_w REAL NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, scene_version_id)
);

CREATE TABLE IF NOT EXISTS asset_documents (
    document_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    uri TEXT NOT NULL,
    content_sha256 TEXT,
    memo TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_records (
    maintenance_record_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
    performed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inspections (
    inspection_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    scene_version_id TEXT REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned', 'in_progress', 'completed', 'cancelled')),
    inspector TEXT NOT NULL,
    scheduled_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inspection_observations (
    inspection_observation_id TEXT PRIMARY KEY,
    inspection_id TEXT NOT NULL REFERENCES inspections(inspection_id) ON DELETE RESTRICT,
    asset_id TEXT REFERENCES assets(asset_id) ON DELETE RESTRICT,
    scene_version_id TEXT REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    result TEXT NOT NULL CHECK(result IN ('conforming', 'nonconforming', 'not_observed')),
    notes TEXT NOT NULL DEFAULT '',
    position_x REAL,
    position_y REAL,
    position_z REAL,
    evidence_document_id TEXT REFERENCES asset_documents(document_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    inspection_id TEXT NOT NULL REFERENCES inspections(inspection_id) ON DELETE RESTRICT,
    inspection_observation_id TEXT REFERENCES inspection_observations(inspection_observation_id) ON DELETE RESTRICT,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    asset_id TEXT REFERENCES assets(asset_id) ON DELETE RESTRICT,
    scene_version_id TEXT REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL CHECK(severity IN ('low', 'medium', 'high', 'critical')),
    status TEXT NOT NULL CHECK(status IN ('open', 'assigned', 'in_progress', 'resolved', 'closed', 'rejected')),
    assignee TEXT,
    due_at TEXT,
    position_x REAL,
    position_y REAL,
    position_z REAL,
    evidence_before_document_id TEXT REFERENCES asset_documents(document_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrective_actions (
    corrective_action_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id) ON DELETE RESTRICT,
    summary TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'assigned', 'in_progress', 'completed', 'rejected')),
    assigned_to TEXT,
    due_at TEXT,
    performed_at TEXT,
    evidence_after_document_id TEXT REFERENCES asset_documents(document_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrective_action_approvals (
    approval_id TEXT PRIMARY KEY,
    corrective_action_id TEXT NOT NULL REFERENCES corrective_actions(corrective_action_id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
    reviewer TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issue_events (
    issue_event_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id) ON DELETE RESTRICT,
    corrective_action_id TEXT REFERENCES corrective_actions(corrective_action_id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_inquiries (
    inquiry_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    question TEXT NOT NULL,
    evidence_snapshot_json TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed')),
    answer TEXT,
    is_unknown INTEGER NOT NULL DEFAULT 0 CHECK(is_unknown IN (0, 1)),
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS evidence_citations (
    citation_id TEXT PRIMARY KEY,
    inquiry_id TEXT NOT NULL REFERENCES evidence_inquiries(inquiry_id) ON DELETE RESTRICT,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    excerpt TEXT NOT NULL,
    UNIQUE(inquiry_id, ordinal)
);

CREATE TABLE IF NOT EXISTS change_candidates (
    change_candidate_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    from_scene_version_id TEXT NOT NULL REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    to_scene_version_id TEXT NOT NULL REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
    change_type TEXT NOT NULL CHECK(change_type IN ('appeared', 'disappeared', 'moved', 'unchanged')),
    before_json TEXT,
    after_json TEXT,
    distance_m REAL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected', 'modified')),
    detection_policy TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(from_scene_version_id, to_scene_version_id, asset_id)
);

CREATE TABLE IF NOT EXISTS change_review_events (
    review_event_id TEXT PRIMARY KEY,
    change_candidate_id TEXT NOT NULL REFERENCES change_candidates(change_candidate_id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected', 'modified')),
    reason TEXT NOT NULL,
    modified_change_json TEXT,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmed_change_events (
    change_event_id TEXT PRIMARY KEY,
    change_candidate_id TEXT NOT NULL UNIQUE REFERENCES change_candidates(change_candidate_id) ON DELETE RESTRICT,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
    change_json TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_event_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL,
    site_id TEXT,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    request_id TEXT,
    result TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_index_jobs (
    job_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    model_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed')),
    output_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS image_features (
    document_id TEXT PRIMARY KEY REFERENCES asset_documents(document_id) ON DELETE CASCADE,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    content_sha256 TEXT NOT NULL,
    model_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    analysis_run_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    scene_version_id TEXT NOT NULL REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    model_id TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    interval_sec REAL NOT NULL CHECK(interval_sec > 0),
    similarity_threshold REAL NOT NULL CHECK(similarity_threshold >= 0 AND similarity_threshold <= 1),
    parameters_json TEXT NOT NULL,
    input_manifest_sha256 TEXT,
    output_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS frame_analyses (
    frame_analysis_id TEXT PRIMARY KEY,
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(analysis_run_id) ON DELETE RESTRICT,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    scene_version_id TEXT NOT NULL REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    image_name TEXT NOT NULL,
    source_frame_index INTEGER NOT NULL CHECK(source_frame_index >= 0),
    source_time_sec REAL NOT NULL CHECK(source_time_sec >= 0),
    camera_id TEXT NOT NULL,
    image_content_sha256 TEXT NOT NULL,
    vlm_input_content_sha256 TEXT NOT NULL,
    thumbnail_content_sha256 TEXT NOT NULL,
    summary TEXT NOT NULL,
    keywords_json TEXT NOT NULL,
    extra_json TEXT NOT NULL,
    position_x REAL NOT NULL,
    position_y REAL NOT NULL,
    position_z REAL NOT NULL,
    rotation_x REAL NOT NULL,
    rotation_y REAL NOT NULL,
    rotation_z REAL NOT NULL,
    rotation_w REAL NOT NULL,
    projection_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(analysis_run_id, image_name)
);

CREATE TABLE IF NOT EXISTS frame_analysis_review_events (
    review_event_id TEXT PRIMARY KEY,
    frame_analysis_id TEXT NOT NULL REFERENCES frame_analyses(frame_analysis_id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('accepted', 'corrected', 'rejected')),
    effective_summary TEXT NOT NULL,
    effective_keywords_json TEXT NOT NULL,
    effective_extra_json TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL,
    inherited_from_review_event_id TEXT REFERENCES frame_analysis_review_events(review_event_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_run_contracts (
    analysis_run_id TEXT PRIMARY KEY REFERENCES analysis_runs(analysis_run_id) ON DELETE RESTRICT,
    analysis_policy_id TEXT NOT NULL,
    prompt_template_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_template_sha256 TEXT NOT NULL,
    output_schema_version TEXT NOT NULL,
    downstream_uses_json TEXT NOT NULL,
    review_required INTEGER NOT NULL CHECK(review_required IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frame_observation_sets (
    observation_set_id TEXT PRIMARY KEY,
    frame_analysis_id TEXT NOT NULL REFERENCES frame_analyses(frame_analysis_id) ON DELETE RESTRICT,
    source_type TEXT NOT NULL CHECK(source_type IN ('model', 'review')),
    source_ref_id TEXT NOT NULL,
    review_event_id TEXT REFERENCES frame_analysis_review_events(review_event_id) ON DELETE RESTRICT,
    analysis_policy_id TEXT NOT NULL,
    output_schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(frame_analysis_id, source_type, source_ref_id)
);

CREATE TABLE IF NOT EXISTS frame_observation_items (
    observation_item_id TEXT PRIMARY KEY,
    observation_set_id TEXT NOT NULL REFERENCES frame_observation_sets(observation_set_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    item_kind TEXT NOT NULL CHECK(item_kind IN (
        'area', 'asset', 'condition', 'safety', 'inspection', 'text', 'signage',
        'caption', 'subject', 'limitation', 'image_quality'
    )),
    subject TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    observation TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL CHECK(severity IN ('info', 'attention', 'critical')),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    attributes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(observation_set_id, ordinal)
);

CREATE TABLE IF NOT EXISTS scene_analysis_activations (
    scene_version_id TEXT PRIMARY KEY REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(analysis_run_id) ON DELETE RESTRICT,
    activated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_analysis_activation_events (
    activation_id TEXT PRIMARY KEY,
    scene_version_id TEXT NOT NULL REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(analysis_run_id) ON DELETE RESTRICT,
    activated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scene_versions_site ON scene_versions(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assets_site ON assets(site_id, name);
CREATE INDEX IF NOT EXISTS idx_observations_asset ON asset_observations(asset_id, created_at);
CREATE INDEX IF NOT EXISTS idx_documents_asset ON asset_documents(asset_id, created_at);
CREATE INDEX IF NOT EXISTS idx_image_jobs_site ON image_index_jobs(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_image_features_site ON image_features(site_id, model_id);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_scene ON analysis_runs(scene_version_id, created_at);
CREATE INDEX IF NOT EXISTS idx_frame_analyses_run ON frame_analyses(analysis_run_id, source_time_sec);
CREATE INDEX IF NOT EXISTS idx_frame_analyses_scene ON frame_analyses(scene_version_id, created_at);
CREATE INDEX IF NOT EXISTS idx_frame_analysis_reviews_frame
    ON frame_analysis_review_events(frame_analysis_id, created_at);
CREATE INDEX IF NOT EXISTS idx_analysis_run_contracts_policy
    ON analysis_run_contracts(analysis_policy_id, output_schema_version);
CREATE INDEX IF NOT EXISTS idx_frame_observation_sets_frame
    ON frame_observation_sets(frame_analysis_id, source_type, created_at);
CREATE INDEX IF NOT EXISTS idx_frame_observation_items_kind
    ON frame_observation_items(item_kind, severity, category, subject);
CREATE INDEX IF NOT EXISTS idx_analysis_activation_events_scene
    ON scene_analysis_activation_events(scene_version_id, activated_at);
CREATE INDEX IF NOT EXISTS idx_captures_site ON captures(site_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_maintenance_asset ON maintenance_records(asset_id, performed_at);
CREATE INDEX IF NOT EXISTS idx_inspections_site ON inspections(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_inspection_observations_inspection
    ON inspection_observations(inspection_id, created_at);
CREATE INDEX IF NOT EXISTS idx_issues_site ON issues(site_id, status, severity, created_at);
CREATE INDEX IF NOT EXISTS idx_issues_inspection ON issues(inspection_id, created_at);
CREATE INDEX IF NOT EXISTS idx_corrective_actions_issue ON corrective_actions(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_corrective_approvals_action
    ON corrective_action_approvals(corrective_action_id, created_at);
CREATE INDEX IF NOT EXISTS idx_issue_events_issue ON issue_events(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_inquiries_site ON evidence_inquiries(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_change_candidates_site ON change_candidates(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_change_reviews_candidate ON change_review_events(change_candidate_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_site ON audit_events(site_id, created_at);
"""


SEARCH_INDEX_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS field_memory_fts USING fts5(
    record_type UNINDEXED,
    record_id UNINDEXED,
    site_id UNINDEXED,
    title,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS field_memory_fts_sites_insert
AFTER INSERT ON sites BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    VALUES ('site', new.site_id, new.site_id, new.name, new.site_id || ' ' || new.description);
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_scene_versions_insert
AFTER INSERT ON scene_versions BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    VALUES ('scene_version', new.scene_version_id, new.site_id, new.label,
            new.scene_version_id || ' ' || new.scene_package_id);
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_assets_insert
AFTER INSERT ON assets BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    VALUES ('asset', new.asset_id, new.site_id, new.name,
            new.asset_id || ' ' || new.asset_type || ' ' || new.status || ' ' || new.description);
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_observations_insert
AFTER INSERT ON asset_observations BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    SELECT 'observation', new.observation_id, assets.site_id,
           assets.name || ' position observation',
           new.observation_id || ' ' || new.asset_id || ' ' || new.scene_version_id || ' ' || new.source
      FROM assets WHERE assets.asset_id = new.asset_id;
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_documents_insert
AFTER INSERT ON asset_documents BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    SELECT 'document', new.document_id, assets.site_id, new.title,
           new.document_id || ' ' || new.asset_id || ' ' || new.kind || ' ' || new.uri || ' ' || new.memo
      FROM assets WHERE assets.asset_id = new.asset_id;
END;

DROP TRIGGER IF EXISTS field_memory_fts_frame_analyses_insert;
CREATE TRIGGER field_memory_fts_frame_analyses_insert
AFTER INSERT ON frame_analyses BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    VALUES ('frame_analysis', new.frame_analysis_id, new.site_id, new.image_name,
            new.frame_analysis_id || ' ' || new.image_name || ' ' || new.summary || ' ' ||
            new.keywords_json || ' ' || new.extra_json);
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_frame_analysis_reviews_insert
AFTER INSERT ON frame_analysis_review_events BEGIN
    DELETE FROM field_memory_fts
     WHERE record_type = 'frame_analysis' AND record_id = new.frame_analysis_id;
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    SELECT 'frame_analysis', frames.frame_analysis_id, frames.site_id, frames.image_name,
           frames.frame_analysis_id || ' ' || frames.image_name || ' ' ||
           new.effective_summary || ' ' || new.effective_keywords_json || ' ' ||
           new.effective_extra_json
      FROM frame_analyses AS frames
     WHERE frames.frame_analysis_id = new.frame_analysis_id;
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_captures_insert
AFTER INSERT ON captures BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    VALUES ('capture', new.capture_id, new.site_id, new.source_type,
            new.capture_id || ' ' || new.memo || ' ' || COALESCE(new.scene_version_id, ''));
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_maintenance_insert
AFTER INSERT ON maintenance_records BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    SELECT 'maintenance_record', new.maintenance_record_id, assets.site_id, new.summary,
           new.maintenance_record_id || ' ' || new.asset_id || ' ' || new.status || ' ' || new.details_json
      FROM assets WHERE assets.asset_id = new.asset_id;
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_inspections_insert
AFTER INSERT ON inspections BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    VALUES ('inspection', new.inspection_id, new.site_id, new.title,
            new.inspection_id || ' ' || new.status || ' ' || new.inspector);
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_issues_insert
AFTER INSERT ON issues BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    VALUES ('issue', new.issue_id, new.site_id, new.title,
            new.issue_id || ' ' || new.description || ' ' || new.severity || ' ' || new.status ||
            ' ' || COALESCE(new.assignee, ''));
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_corrective_actions_insert
AFTER INSERT ON corrective_actions BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    SELECT 'corrective_action', new.corrective_action_id, issues.site_id, new.summary,
           new.corrective_action_id || ' ' || new.status || ' ' || COALESCE(new.assigned_to, '')
      FROM issues WHERE issues.issue_id = new.issue_id;
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_inspections_update
AFTER UPDATE ON inspections BEGIN
    DELETE FROM field_memory_fts WHERE record_type = 'inspection' AND record_id = old.inspection_id;
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    VALUES ('inspection', new.inspection_id, new.site_id, new.title,
            new.inspection_id || ' ' || new.status || ' ' || new.inspector);
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_issues_update
AFTER UPDATE ON issues BEGIN
    DELETE FROM field_memory_fts WHERE record_type = 'issue' AND record_id = old.issue_id;
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    VALUES ('issue', new.issue_id, new.site_id, new.title,
            new.issue_id || ' ' || new.description || ' ' || new.severity || ' ' || new.status ||
            ' ' || COALESCE(new.assignee, ''));
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_corrective_actions_update
AFTER UPDATE ON corrective_actions BEGIN
    DELETE FROM field_memory_fts
     WHERE record_type = 'corrective_action' AND record_id = old.corrective_action_id;
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    SELECT 'corrective_action', new.corrective_action_id, issues.site_id, new.summary,
           new.corrective_action_id || ' ' || new.status || ' ' || COALESCE(new.assigned_to, '')
      FROM issues WHERE issues.issue_id = new.issue_id;
END;

CREATE TRIGGER IF NOT EXISTS field_memory_fts_confirmed_changes_insert
AFTER INSERT ON confirmed_change_events BEGIN
    INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
    VALUES ('change_event', new.change_event_id, new.site_id, 'confirmed change',
            new.change_event_id || ' ' || new.asset_id || ' ' || new.change_json);
END;
"""


SEARCH_INDEX_REBUILD_SQL = """
DELETE FROM field_memory_fts;
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'site', site_id, site_id, name, site_id || ' ' || description FROM sites;
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'scene_version', scene_version_id, site_id, label,
       scene_version_id || ' ' || scene_package_id FROM scene_versions;
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'asset', asset_id, site_id, name,
       asset_id || ' ' || asset_type || ' ' || status || ' ' || description FROM assets;
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'observation', observations.observation_id, assets.site_id,
       assets.name || ' position observation',
       observations.observation_id || ' ' || observations.asset_id || ' ' ||
       observations.scene_version_id || ' ' || observations.source
  FROM asset_observations AS observations
  JOIN assets ON assets.asset_id = observations.asset_id;
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'document', documents.document_id, assets.site_id, documents.title,
       documents.document_id || ' ' || documents.asset_id || ' ' || documents.kind || ' ' ||
       documents.uri || ' ' || documents.memo
  FROM asset_documents AS documents
  JOIN assets ON assets.asset_id = documents.asset_id;
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'frame_analysis', frame_analysis_id, site_id, image_name,
       frame_analysis_id || ' ' || image_name || ' ' ||
       COALESCE((
           SELECT reviews.effective_summary
             FROM frame_analysis_review_events AS reviews
            WHERE reviews.frame_analysis_id = frame_analyses.frame_analysis_id
            ORDER BY reviews.created_at DESC, reviews.rowid DESC LIMIT 1
       ), summary) || ' ' ||
       COALESCE((
           SELECT reviews.effective_keywords_json
             FROM frame_analysis_review_events AS reviews
            WHERE reviews.frame_analysis_id = frame_analyses.frame_analysis_id
            ORDER BY reviews.created_at DESC, reviews.rowid DESC LIMIT 1
       ), keywords_json) || ' ' ||
       COALESCE((
           SELECT reviews.effective_extra_json
             FROM frame_analysis_review_events AS reviews
            WHERE reviews.frame_analysis_id = frame_analyses.frame_analysis_id
            ORDER BY reviews.created_at DESC, reviews.rowid DESC LIMIT 1
       ), extra_json)
  FROM frame_analyses;
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'capture', capture_id, site_id, source_type,
       capture_id || ' ' || memo || ' ' || COALESCE(scene_version_id, '') FROM captures;
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'maintenance_record', records.maintenance_record_id, assets.site_id, records.summary,
       records.maintenance_record_id || ' ' || records.asset_id || ' ' || records.status || ' ' || records.details_json
  FROM maintenance_records AS records JOIN assets USING(asset_id);
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'inspection', inspection_id, site_id, title,
       inspection_id || ' ' || status || ' ' || inspector FROM inspections;
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'issue', issue_id, site_id, title,
       issue_id || ' ' || description || ' ' || severity || ' ' || status || ' ' || COALESCE(assignee, '')
  FROM issues;
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'corrective_action', actions.corrective_action_id, issues.site_id, actions.summary,
       actions.corrective_action_id || ' ' || actions.status || ' ' || COALESCE(actions.assigned_to, '')
  FROM corrective_actions AS actions JOIN issues USING(issue_id);
INSERT INTO field_memory_fts(record_type, record_id, site_id, title, body)
SELECT 'change_event', change_event_id, site_id, 'confirmed change',
       change_event_id || ' ' || asset_id || ' ' || change_json FROM confirmed_change_events;
"""


SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    migration_sha256 TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

REQUIRED_SCHEMA_TABLES = frozenset(
    {
        "sites",
        "scene_versions",
        "captures",
        "assets",
        "asset_observations",
        "asset_documents",
        "maintenance_records",
        "evidence_inquiries",
        "evidence_citations",
        "change_candidates",
        "change_review_events",
        "confirmed_change_events",
        "audit_events",
        "image_index_jobs",
        "image_features",
        "analysis_runs",
        "frame_analyses",
        "frame_analysis_review_events",
        "analysis_run_contracts",
        "frame_observation_sets",
        "frame_observation_items",
        "scene_analysis_activations",
        "scene_analysis_activation_events",
        "inspections",
        "inspection_observations",
        "issues",
        "corrective_actions",
        "corrective_action_approvals",
        "issue_events",
        "field_memory_fts",
        "schema_migrations",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(value: Any, field: str, *, prefix: str) -> str:
    if value is None or value == "":
        return f"{prefix}_{uuid.uuid4().hex}"
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"field_memory_{field}_invalid")
    return value


def _text(value: Any, field: str, *, required: bool = True, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise ValueError(f"field_memory_{field}_must_be_string")
    result = value.strip()
    if required and not result:
        raise ValueError(f"field_memory_{field}_required")
    if len(result) > maximum or any(
        ord(character) < 32 and character not in "\t\n\r" for character in result
    ):
        raise ValueError(f"field_memory_{field}_invalid")
    return result


def _optional_timestamp(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    result = _text(value, field, maximum=64)
    try:
        datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"field_memory_{field}_invalid") from error
    return result


def _required_timestamp(value: Any, field: str) -> str:
    result = _optional_timestamp(value, field)
    if result is None:
        raise ValueError(f"field_memory_{field}_required")
    return result


def _json_data(value: Any, field: str, *, maximum: int = 64_000) -> str:
    if value is None:
        value = {}
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"field_memory_{field}_invalid") from error
    if len(result.encode("utf-8")) > maximum:
        raise ValueError(f"field_memory_{field}_too_large")
    return result


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"field_memory_{field}_must_be_number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"field_memory_{field}_must_be_finite")
    return result


def _pose(value: Any) -> tuple[float, float, float, float, float, float, float]:
    if not isinstance(value, dict):
        raise ValueError("field_memory_pose_must_be_object")
    position = value.get("position")
    rotation = value.get("rotation")
    if not isinstance(position, dict) or not isinstance(rotation, dict):
        raise ValueError("field_memory_pose_position_rotation_required")
    px, py, pz = (
        _finite_number(position.get(axis), f"position_{axis}") for axis in "xyz"
    )
    qx, qy, qz, qw = (
        _finite_number(rotation.get(axis), f"rotation_{axis}") for axis in "xyzw"
    )
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if norm == 0.0 or abs(norm - 1.0) > QUATERNION_TOLERANCE:
        raise ValueError(
            "field_memory_pose_rotation_quaternion_not_unit:tolerance=1e-6"
        )
    return px, py, pz, qx, qy, qz, qw


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


class FieldMemoryService:
    """Store field registry metadata without mutating spatial artifacts."""

    def __init__(
        self,
        repo_root: Path,
        scene_packages_provider: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.repository_layout = SceneRepositoryLayout.from_root(self.repo_root)
        self.storage_root = self.repository_layout.site_records
        self.database_path = self.repository_layout.site_registry
        self.attachments_root = self.repository_layout.site_attachments
        self.staging_root = self.repository_layout.site_record_staging
        self.scene_packages_provider = scene_packages_provider

    @staticmethod
    def _schema_tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }

    @staticmethod
    def _migration_sha256(from_version: int) -> str:
        payload = "\n".join(
            (
                f"field-memory:{from_version}->{SCHEMA_VERSION}",
                SCHEMA_SQL,
                SEARCH_INDEX_SQL,
                SEARCH_INDEX_REBUILD_SQL,
                SCHEMA_MIGRATIONS_SQL,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _backup_before_migration(
        self,
        connection: sqlite3.Connection,
        database_path: Path,
        from_version: int,
    ) -> Path:
        backup_root = database_path.parent / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_root / (
            f"{database_path.stem}-schema-v{from_version}-{timestamp}-{uuid.uuid4().hex[:8]}.sqlite3"
        )
        backup = sqlite3.connect(backup_path)
        try:
            connection.backup(backup)
            backup.commit()
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("field_memory_schema_backup_integrity_failed")
        except BaseException:
            backup.close()
            backup_path.unlink(missing_ok=True)
            raise
        backup.close()
        return backup_path

    def create_database_backup(self, label: str) -> Path:
        """Create and verify a point-in-time copy of the writable registry.

        Destructive feature operations use this public boundary instead of
        copying SQLite files directly.  ``Connection.backup`` includes the
        committed WAL state and therefore remains valid while readers are
        active.
        """

        normalized_label = str(label).strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", normalized_label) is None:
            raise ValueError("field_memory_backup_label_invalid")
        backup_root = self.database_path.parent / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_root / (
            f"{self.database_path.stem}-{normalized_label}-{timestamp}-"
            f"{uuid.uuid4().hex[:8]}.sqlite3"
        )
        with self._connection() as connection:
            backup = sqlite3.connect(backup_path)
            try:
                connection.backup(backup)
                backup.commit()
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("field_memory_database_backup_integrity_failed")
            except BaseException:
                backup.close()
                backup_path.unlink(missing_ok=True)
                raise
            backup.close()
        return backup_path

    def _verify_latest_schema(self, connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("field_memory_database_integrity_failed")
        missing = sorted(REQUIRED_SCHEMA_TABLES - self._schema_tables(connection))
        if missing:
            raise RuntimeError("field_memory_schema_drift_missing:" + ",".join(missing))

    @staticmethod
    def _migrate_analysis_runs_cancelled_status(connection: sqlite3.Connection) -> None:
        """Rebuild v8 analysis_runs so cancellation is a first-class status."""

        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'analysis_runs'"
        ).fetchone()
        if row is None or "'cancelled'" in str(row[0]):
            return
        columns = (
            "analysis_run_id, site_id, scene_version_id, model_id, model_revision, "
            "prompt_version, status, interval_sec, similarity_threshold, parameters_json, "
            "input_manifest_sha256, output_count, failure_reason, created_at, started_at, completed_at"
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(f"""
BEGIN IMMEDIATE;
CREATE TABLE analysis_runs_v9 (
    analysis_run_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE RESTRICT,
    scene_version_id TEXT NOT NULL REFERENCES scene_versions(scene_version_id) ON DELETE RESTRICT,
    model_id TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    interval_sec REAL NOT NULL CHECK(interval_sec > 0),
    similarity_threshold REAL NOT NULL CHECK(similarity_threshold >= 0 AND similarity_threshold <= 1),
    parameters_json TEXT NOT NULL,
    input_manifest_sha256 TEXT,
    output_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
INSERT INTO analysis_runs_v9 ({columns}) SELECT
    analysis_run_id, site_id, scene_version_id, model_id, model_revision, prompt_version,
    CASE WHEN status = 'failed' AND failure_reason = 'cancelled_by_operator'
         THEN 'cancelled' ELSE status END,
    interval_sec, similarity_threshold, parameters_json, input_manifest_sha256, output_count,
    failure_reason, created_at, started_at, completed_at
FROM analysis_runs;
DROP TABLE analysis_runs;
ALTER TABLE analysis_runs_v9 RENAME TO analysis_runs;
CREATE INDEX IF NOT EXISTS idx_analysis_runs_scene ON analysis_runs(scene_version_id, created_at);
COMMIT;
""")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
        violations = list(connection.execute("PRAGMA foreign_key_check"))
        if violations:
            raise RuntimeError("field_memory_analysis_run_migration_foreign_key_failed")

    def _migrate_schema(
        self,
        connection: sqlite3.Connection,
        database_path: Path,
        *,
        create_backup: bool = True,
    ) -> None:
        """Migrate a writable overlay to the latest schema atomically.

        Versions 1 through 8 are supported compatibility inputs.  Version 0
        is a new empty database.  Immutable package snapshots are never
        modified; they are copied into a freshly created latest-schema overlay.
        """

        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version == SCHEMA_VERSION:
            self._verify_latest_schema(connection)
            return
        if current_version != 0 and not (
            MINIMUM_MIGRATABLE_SCHEMA_VERSION <= current_version < SCHEMA_VERSION
        ):
            raise RuntimeError(
                f"field_memory_schema_version_unsupported:{current_version}"
            )
        existing_tables = {
            name
            for name in self._schema_tables(connection)
            if not name.startswith("sqlite_")
        }
        is_empty_bootstrap = current_version == 0 and not existing_tables
        if not is_empty_bootstrap and create_backup:
            self._backup_before_migration(connection, database_path, current_version)
        if current_version == 8 and "analysis_runs" in existing_tables:
            self._migrate_analysis_runs_cancelled_status(connection)

        migration_id = (
            f"bootstrap-v{SCHEMA_VERSION}"
            if is_empty_bootstrap
            else f"compat-v{current_version}-to-v{SCHEMA_VERSION}"
        )
        checksum = self._migration_sha256(current_version)
        applied_at = _now().replace("'", "''")
        script = "\n".join(
            (
                "BEGIN IMMEDIATE;",
                SCHEMA_SQL,
                SEARCH_INDEX_SQL,
                SEARCH_INDEX_REBUILD_SQL,
                SCHEMA_MIGRATIONS_SQL,
                (
                    "INSERT OR IGNORE INTO schema_migrations "
                    "(migration_id, from_version, to_version, migration_sha256, applied_at) "
                    f"VALUES ('{migration_id}', {current_version}, {SCHEMA_VERSION}, "
                    f"'{checksum}', '{applied_at}');"
                ),
                f"PRAGMA user_version = {SCHEMA_VERSION};",
                "COMMIT;",
            )
        )
        try:
            connection.executescript(script)
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        self._verify_latest_schema(connection)

    def _embedded_field_memory_snapshots(self) -> list[Path]:
        runtime = (
            self.repo_root
            / "web_viewer"
            / "app"
            / "public"
            / "scene_package"
            / "runtime"
        )
        if not runtime.is_dir():
            return []
        return sorted(runtime.glob("*/dataset/field_memory/field_memory.sqlite3"))

    @staticmethod
    def _copy_snapshot_table(
        source: sqlite3.Connection,
        target: sqlite3.Connection,
        table: str,
    ) -> None:
        source_tables = {
            str(row[0])
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if table not in source_tables:
            return
        rows = source.execute(f"SELECT * FROM {table}")
        columns = [str(item[0]) for item in rows.description or []]
        if not columns:
            return
        marks = ",".join("?" for _ in columns)
        statement = (
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({marks})"
        )
        for row in rows:
            target.execute(statement, tuple(row))

    def _bootstrap_embedded_field_memory(self) -> None:
        """Initialize a standalone app from its bundled Scene Package datasets.

        The embedded databases remain immutable snapshots.  They are merged
        once into the application's writable overlay so existing APIs and
        workers can add records without writing into a Scene Package.
        """

        snapshots = self._embedded_field_memory_snapshots()
        if not snapshots or self.database_path.exists():
            return
        self.storage_root.mkdir(parents=True, exist_ok=True)
        temporary = self.database_path.with_name(
            f".{self.database_path.name}.{os.getpid()}.bootstrap"
        )
        temporary.unlink(missing_ok=True)
        target = sqlite3.connect(temporary)
        try:
            target.execute("PRAGMA foreign_keys = ON")
            self._migrate_schema(target, temporary, create_backup=False)
            table_order = (
                "sites",
                "scene_versions",
                "captures",
                "assets",
                "asset_observations",
                "asset_documents",
                "maintenance_records",
                "inspections",
                "inspection_observations",
                "issues",
                "corrective_actions",
                "corrective_action_approvals",
                "issue_events",
                "evidence_inquiries",
                "evidence_citations",
                "image_index_jobs",
                "image_features",
                "analysis_runs",
                "frame_analyses",
                "frame_analysis_review_events",
                "analysis_run_contracts",
                "frame_observation_sets",
                "frame_observation_items",
                "scene_analysis_activations",
                "scene_analysis_activation_events",
                "change_candidates",
                "change_review_events",
                "confirmed_change_events",
                "audit_events",
            )
            for snapshot in snapshots:
                source = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
                try:
                    if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError(
                            f"field_memory_embedded_snapshot_invalid:{snapshot}"
                        )
                    source_version = int(
                        source.execute("PRAGMA user_version").fetchone()[0]
                    )
                    if source_version != 0 and not (
                        MINIMUM_MIGRATABLE_SCHEMA_VERSION
                        <= source_version
                        <= SCHEMA_VERSION
                    ):
                        raise RuntimeError(
                            f"field_memory_embedded_schema_version_unsupported:{source_version}:{snapshot}"
                        )
                    for table in table_order:
                        self._copy_snapshot_table(source, target, table)
                finally:
                    source.close()
            target.executescript(SEARCH_INDEX_REBUILD_SQL)
            target.commit()
        finally:
            target.close()
        if self.database_path.exists():
            temporary.unlink(missing_ok=True)
            return
        os.replace(temporary, self.database_path)

        destination_root = self.attachments_root
        for snapshot in snapshots:
            package_content = snapshot.parent / "content" / "sha256"
            if not package_content.is_dir():
                continue
            for source in package_content.glob("*/*"):
                if not source.is_file() or source.is_symlink():
                    raise RuntimeError(
                        f"field_memory_embedded_content_invalid:{source}"
                    )
                destination = destination_root / source.parent.name / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    continue
                try:
                    os.link(source, destination)
                except OSError:
                    shutil.copy2(source, destination)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.database_path.exists():
            self.storage_root.mkdir(parents=True, exist_ok=True)
            self._bootstrap_embedded_field_memory()
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            self._migrate_schema(connection, self.database_path)
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except sqlite3.IntegrityError as error:
            if write:
                connection.rollback()
            raise FileExistsError(
                "field_memory_record_already_exists_or_conflicts"
            ) from error
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_writable_overlay(self) -> Path:
        """Create and validate the writable registry overlay, if necessary.

        Standalone distributions use this entry point after copying immutable
        Scene Package snapshots.  It intentionally materializes the registry
        beside, rather than inside, those packages so later application writes
        cannot mutate a distributed Scene Package.
        """

        with self._connection():
            pass
        return self.database_path

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """Expose a schema-checked read connection to query-only adapters."""

        with self._connection() as connection:
            yield connection

    def _verified_scene_packages(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        if self.scene_packages_provider is not None:
            for item in self.scene_packages_provider():
                package_id = item.get("scene_package_id")
                if isinstance(package_id, str) and item.get("status") == "verified":
                    result[package_id] = item
        index_path = (
            self.repo_root
            / "web_viewer"
            / "app"
            / "public"
            / "scene_package"
            / "distribution_index.json"
        )
        if index_path.is_file():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                index = {}
            entries = (
                index
                if isinstance(index, list)
                else index.get("packages", [])
                if isinstance(index, dict)
                else []
            )
            if isinstance(entries, list):
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    package_id = item.get("scene_package_id")
                    if (
                        isinstance(package_id, str)
                        and item.get("status", "verified") == "verified"
                    ):
                        result.setdefault(package_id, item)
        return result

    def verified_scene_packages(self) -> list[dict[str, Any]]:
        """Return the minimal read-only package catalog needed by this feature.

        A standalone field-memory application must not require the broader
        scene-manager API merely to select an already verified package.
        """

        packages: list[dict[str, Any]] = []
        for package_id, item in self._verified_scene_packages().items():
            packages.append(
                {
                    "scene_package_id": package_id,
                    "scene_id": item.get("scene_id"),
                    "project_name": item.get("project_name"),
                    "status": "verified",
                }
            )
        return sorted(packages, key=lambda item: item["scene_package_id"])

    def summary(self) -> dict[str, Any]:
        with self._connection() as connection:
            counts = {
                name: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for name, table in (
                    ("sites", "sites"),
                    ("scene_versions", "scene_versions"),
                    ("assets", "assets"),
                    ("observations", "asset_observations"),
                    ("documents", "asset_documents"),
                    ("captures", "captures"),
                    ("maintenance_records", "maintenance_records"),
                    ("inspections", "inspections"),
                    ("inspection_observations", "inspection_observations"),
                    ("issues", "issues"),
                    ("corrective_actions", "corrective_actions"),
                    ("corrective_action_approvals", "corrective_action_approvals"),
                    ("issue_events", "issue_events"),
                    ("analysis_runs", "analysis_runs"),
                    ("frame_analyses", "frame_analyses"),
                    ("frame_analysis_reviews", "frame_analysis_review_events"),
                    ("analysis_run_contracts", "analysis_run_contracts"),
                    ("frame_observation_sets", "frame_observation_sets"),
                    ("frame_observation_items", "frame_observation_items"),
                    ("analysis_activations", "scene_analysis_activations"),
                    ("evidence_inquiries", "evidence_inquiries"),
                    ("change_candidates", "change_candidates"),
                    ("confirmed_changes", "confirmed_change_events"),
                    ("audit_events", "audit_events"),
                )
            }
        return {"schema_version": str(SCHEMA_VERSION), "counts": counts}

    def sites(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM sites ORDER BY name, site_id"
                )
            ]

    def create_site(self, payload: dict[str, Any]) -> dict[str, Any]:
        site_id = _identifier(payload.get("site_id"), "site_id", prefix="site")
        name = _text(payload.get("name"), "site_name", maximum=120)
        description = _text(
            payload.get("description", ""),
            "site_description",
            required=False,
            maximum=2000,
        )
        created_at = _now()
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO sites(site_id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (site_id, name, description, created_at, created_at),
            )
        return {
            "site_id": site_id,
            "name": name,
            "description": description,
            "created_at": created_at,
            "updated_at": created_at,
        }

    def site(self, site_id: str) -> dict[str, Any]:
        _identifier(site_id, "site_id", prefix="site")
        with self._connection() as connection:
            site = connection.execute(
                "SELECT * FROM sites WHERE site_id = ?", (site_id,)
            ).fetchone()
            if site is None:
                raise FileNotFoundError(f"field_memory_site_not_found:{site_id}")
            result = _row(site)
            result["scene_versions"] = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM scene_versions WHERE site_id = ? ORDER BY created_at DESC",
                    (site_id,),
                )
            ]
            result["assets"] = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM assets WHERE site_id = ? ORDER BY name, asset_id",
                    (site_id,),
                )
            ]
            return result

    def scene_versions(self, site_id: str) -> list[dict[str, Any]]:
        self.site(site_id)
        with self._connection() as connection:
            return [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM scene_versions WHERE site_id = ? ORDER BY created_at DESC",
                    (site_id,),
                )
            ]

    def create_scene_version(
        self, site_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.site(site_id)
        scene_version_id = _identifier(
            payload.get("scene_version_id"), "scene_version_id", prefix="scene"
        )
        scene_package_id = _identifier(
            _text(payload.get("scene_package_id"), "scene_package_id", maximum=96),
            "scene_package_id",
            prefix="package",
        )
        label = _text(
            payload.get("label", scene_package_id), "scene_version_label", maximum=160
        )
        captured_at = _optional_timestamp(payload.get("captured_at"), "captured_at")
        created_at = _now()
        with scene_package_reference_lock(self.repo_root, scene_package_id):
            # Recheck while holding the same cross-process lock used by Scene
            # Package removal. This closes the validate-then-delete race that
            # could otherwise create a dangling SceneVersion reference.
            package = self._verified_scene_packages().get(scene_package_id)
            if package is None:
                raise ValueError(
                    f"field_memory_scene_package_not_verified:{scene_package_id}"
                )
            with self._connection(write=True) as connection:
                connection.execute(
                    "INSERT INTO scene_versions(scene_version_id, site_id, scene_package_id, label, captured_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        scene_version_id,
                        site_id,
                        scene_package_id,
                        label,
                        captured_at,
                        created_at,
                    ),
                )
        return {
            "scene_version_id": scene_version_id,
            "site_id": site_id,
            "scene_package_id": scene_package_id,
            "label": label,
            "captured_at": captured_at,
            "created_at": created_at,
        }

    def captures(self, site_id: str) -> list[dict[str, Any]]:
        self.site(site_id)
        with self._connection() as connection:
            return [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM captures WHERE site_id = ? ORDER BY captured_at DESC, created_at DESC",
                    (site_id,),
                )
            ]

    def create_capture(self, site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.site(site_id)
        capture_id = _identifier(
            payload.get("capture_id"), "capture_id", prefix="capture"
        )
        source_type = _text(
            payload.get("source_type", "video"), "capture_source_type", maximum=64
        )
        scene_version_id = payload.get("scene_version_id")
        if scene_version_id not in (None, ""):
            scene_version_id = _identifier(
                scene_version_id, "scene_version_id", prefix="scene"
            )
        else:
            scene_version_id = None
        source_sha = payload.get("source_content_sha256")
        if source_sha not in (None, ""):
            if (
                not isinstance(source_sha, str)
                or SHA256_RE.fullmatch(source_sha) is None
            ):
                raise ValueError("field_memory_capture_source_content_sha256_invalid")
            source_sha = source_sha.lower()
        else:
            source_sha = None
        captured_at = _required_timestamp(
            payload.get("captured_at"), "capture_captured_at"
        )
        memo = _text(
            payload.get("memo", ""), "capture_memo", required=False, maximum=4000
        )
        created_at = _now()
        with self._connection(write=True) as connection:
            if scene_version_id is not None:
                scene = connection.execute(
                    "SELECT site_id FROM scene_versions WHERE scene_version_id = ?",
                    (scene_version_id,),
                ).fetchone()
                if scene is None:
                    raise FileNotFoundError(
                        f"field_memory_scene_version_not_found:{scene_version_id}"
                    )
                if scene["site_id"] != site_id:
                    raise ValueError("field_memory_capture_site_mismatch")
            connection.execute(
                "INSERT INTO captures(capture_id, site_id, scene_version_id, source_type, "
                "source_content_sha256, captured_at, memo, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    capture_id,
                    site_id,
                    scene_version_id,
                    source_type,
                    source_sha,
                    captured_at,
                    memo,
                    created_at,
                ),
            )
        return {
            "capture_id": capture_id,
            "site_id": site_id,
            "scene_version_id": scene_version_id,
            "source_type": source_type,
            "source_content_sha256": source_sha,
            "captured_at": captured_at,
            "memo": memo,
            "created_at": created_at,
        }

    def assets(self, site_id: str) -> list[dict[str, Any]]:
        self.site(site_id)
        with self._connection() as connection:
            return [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM assets WHERE site_id = ? ORDER BY name, asset_id",
                    (site_id,),
                )
            ]

    def asset(self, asset_id: str) -> dict[str, Any]:
        """Return one asset including its owning site for boundary checks."""
        return _row(self._asset(asset_id))

    def create_asset(self, site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.site(site_id)
        asset_id = _identifier(payload.get("asset_id"), "asset_id", prefix="asset")
        name = _text(payload.get("name"), "asset_name", maximum=160)
        asset_type = _text(
            payload.get("asset_type", "equipment"), "asset_type", maximum=64
        )
        status = _text(payload.get("status", "active"), "asset_status", maximum=64)
        description = _text(
            payload.get("description", ""),
            "asset_description",
            required=False,
            maximum=2000,
        )
        created_at = _now()
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO assets(asset_id, site_id, name, asset_type, status, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    asset_id,
                    site_id,
                    name,
                    asset_type,
                    status,
                    description,
                    created_at,
                    created_at,
                ),
            )
        return {
            "asset_id": asset_id,
            "site_id": site_id,
            "name": name,
            "asset_type": asset_type,
            "status": status,
            "description": description,
            "created_at": created_at,
            "updated_at": created_at,
        }

    @staticmethod
    def _optional_id(value: Any, field: str, *, prefix: str) -> str | None:
        if value in (None, ""):
            return None
        return _identifier(value, field, prefix=prefix)

    @staticmethod
    def _optional_position(
        value: Any,
    ) -> tuple[float | None, float | None, float | None]:
        if value in (None, ""):
            return None, None, None
        if not isinstance(value, dict):
            raise ValueError("field_memory_position_must_be_object")
        return tuple(
            _finite_number(value.get(axis), f"position_{axis}") for axis in "xyz"
        )  # type: ignore[return-value]

    @staticmethod
    def _require_site_link(
        connection: sqlite3.Connection,
        *,
        site_id: str,
        scene_version_id: str | None = None,
        asset_id: str | None = None,
        document_id: str | None = None,
    ) -> None:
        if scene_version_id is not None:
            row = connection.execute(
                "SELECT site_id FROM scene_versions WHERE scene_version_id = ?",
                (scene_version_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"field_memory_scene_version_not_found:{scene_version_id}"
                )
            if row["site_id"] != site_id:
                raise ValueError("field_memory_scene_version_site_mismatch")
        if asset_id is not None:
            row = connection.execute(
                "SELECT site_id FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"field_memory_asset_not_found:{asset_id}")
            if row["site_id"] != site_id:
                raise ValueError("field_memory_asset_site_mismatch")
        if document_id is not None:
            row = connection.execute(
                "SELECT assets.site_id, documents.asset_id FROM asset_documents AS documents "
                "JOIN assets USING(asset_id) WHERE documents.document_id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"field_memory_document_not_found:{document_id}"
                )
            if row["site_id"] != site_id or (
                asset_id is not None and row["asset_id"] != asset_id
            ):
                raise ValueError("field_memory_document_site_or_asset_mismatch")

    def inspections(self, site_id: str) -> list[dict[str, Any]]:
        self.site(site_id)
        with self._connection() as connection:
            return [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM inspections WHERE site_id = ? ORDER BY created_at DESC",
                    (site_id,),
                )
            ]

    def create_inspection(
        self, site_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.site(site_id)
        inspection_id = _identifier(
            payload.get("inspection_id"), "inspection_id", prefix="inspection"
        )
        scene_version_id = self._optional_id(
            payload.get("scene_version_id"), "scene_version_id", prefix="scene"
        )
        title = _text(payload.get("title"), "inspection_title", maximum=240)
        inspector = _text(
            payload.get("inspector", "unassigned"), "inspection_inspector", maximum=160
        )
        scheduled_at = _optional_timestamp(
            payload.get("scheduled_at"), "inspection_scheduled_at"
        )
        created_at = _now()
        with self._connection(write=True) as connection:
            self._require_site_link(
                connection, site_id=site_id, scene_version_id=scene_version_id
            )
            connection.execute(
                "INSERT INTO inspections(inspection_id, site_id, scene_version_id, title, status, inspector, "
                "scheduled_at, started_at, completed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'planned', ?, ?, NULL, NULL, ?, ?)",
                (
                    inspection_id,
                    site_id,
                    scene_version_id,
                    title,
                    inspector,
                    scheduled_at,
                    created_at,
                    created_at,
                ),
            )
        return self.inspection(inspection_id)

    def inspection(self, inspection_id: str) -> dict[str, Any]:
        _identifier(inspection_id, "inspection_id", prefix="inspection")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM inspections WHERE inspection_id = ?", (inspection_id,)
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"field_memory_inspection_not_found:{inspection_id}"
                )
            result = _row(row)
            result["observations"] = [
                _row(item)
                for item in connection.execute(
                    "SELECT * FROM inspection_observations WHERE inspection_id = ? ORDER BY created_at",
                    (inspection_id,),
                )
            ]
            result["issues"] = [
                _row(item)
                for item in connection.execute(
                    "SELECT * FROM issues WHERE inspection_id = ? ORDER BY severity DESC, created_at",
                    (inspection_id,),
                )
            ]
            return result

    def transition_inspection(
        self, inspection_id: str, payload: dict[str, Any], *, actor_id: str
    ) -> dict[str, Any]:
        target = _text(payload.get("status"), "inspection_status", maximum=32)
        reason = _text(
            payload.get("reason", ""),
            "inspection_transition_reason",
            required=False,
            maximum=2000,
        )
        transitions = {
            "planned": {"in_progress", "cancelled"},
            "in_progress": {"completed", "cancelled"},
            "completed": set(),
            "cancelled": set(),
        }
        now = _now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM inspections WHERE inspection_id = ?", (inspection_id,)
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"field_memory_inspection_not_found:{inspection_id}"
                )
            current = str(row["status"])
            if target not in transitions[current]:
                raise ValueError(
                    f"field_memory_inspection_transition_invalid:{current}->{target}"
                )
            if target == "completed":
                unresolved = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM issues WHERE inspection_id = ? AND status NOT IN ('closed', 'rejected')",
                        (inspection_id,),
                    ).fetchone()[0]
                )
                if unresolved:
                    raise ValueError("field_memory_inspection_has_unresolved_issues")
            started_at = now if target == "in_progress" else row["started_at"]
            completed_at = (
                now if target in {"completed", "cancelled"} else row["completed_at"]
            )
            connection.execute(
                "UPDATE inspections SET status = ?, started_at = ?, completed_at = ?, updated_at = ? "
                "WHERE inspection_id = ?",
                (target, started_at, completed_at, now, inspection_id),
            )
            connection.execute(
                "INSERT INTO audit_events VALUES (?, ?, 'operator', ?, ?, ?, NULL, 'success', ?, ?)",
                (
                    f"audit_{uuid.uuid4().hex}",
                    actor_id,
                    row["site_id"],
                    "inspection.transition",
                    inspection_id,
                    _json_data(
                        {"from": current, "to": target, "reason": reason},
                        "audit_details",
                    ),
                    now,
                ),
            )
        return self.inspection(inspection_id)

    def create_inspection_observation(
        self, inspection_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        inspection = self.inspection(inspection_id)
        if inspection["status"] != "in_progress":
            raise ValueError("field_memory_inspection_observation_requires_in_progress")
        observation_id = _identifier(
            payload.get("inspection_observation_id"),
            "inspection_observation_id",
            prefix="inspection-observation",
        )
        result = _text(
            payload.get("result"), "inspection_observation_result", maximum=32
        )
        if result not in {"conforming", "nonconforming", "not_observed"}:
            raise ValueError("field_memory_inspection_observation_result_invalid")
        asset_id = self._optional_id(
            payload.get("asset_id"), "asset_id", prefix="asset"
        )
        scene_version_id = self._optional_id(
            payload.get("scene_version_id", inspection.get("scene_version_id")),
            "scene_version_id",
            prefix="scene",
        )
        evidence_id = self._optional_id(
            payload.get("evidence_document_id"), "document_id", prefix="document"
        )
        notes = _text(
            payload.get("notes", ""),
            "inspection_observation_notes",
            required=False,
            maximum=4000,
        )
        px, py, pz = self._optional_position(payload.get("position"))
        created_at = _now()
        with self._connection(write=True) as connection:
            self._require_site_link(
                connection,
                site_id=inspection["site_id"],
                scene_version_id=scene_version_id,
                asset_id=asset_id,
                document_id=evidence_id,
            )
            connection.execute(
                "INSERT INTO inspection_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation_id,
                    inspection_id,
                    asset_id,
                    scene_version_id,
                    result,
                    notes,
                    px,
                    py,
                    pz,
                    evidence_id,
                    created_at,
                ),
            )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM inspection_observations WHERE inspection_observation_id = ?",
                (observation_id,),
            ).fetchone()
            assert row is not None
            return _row(row)

    def issues(
        self, site_id: str, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        self.site(site_id)
        with self._connection() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM issues WHERE site_id = ? AND status = ? ORDER BY created_at DESC",
                    (site_id, status),
                )
            else:
                rows = connection.execute(
                    "SELECT * FROM issues WHERE site_id = ? ORDER BY created_at DESC",
                    (site_id,),
                )
            return [_row(row) for row in rows]

    def create_issue(
        self, site_id: str, payload: dict[str, Any], *, actor_id: str
    ) -> dict[str, Any]:
        self.site(site_id)
        issue_id = _identifier(payload.get("issue_id"), "issue_id", prefix="issue")
        inspection_id = _identifier(
            payload.get("inspection_id"), "inspection_id", prefix="inspection"
        )
        observation_id = self._optional_id(
            payload.get("inspection_observation_id"),
            "inspection_observation_id",
            prefix="inspection-observation",
        )
        asset_id = self._optional_id(
            payload.get("asset_id"), "asset_id", prefix="asset"
        )
        scene_version_id = self._optional_id(
            payload.get("scene_version_id"), "scene_version_id", prefix="scene"
        )
        evidence_id = self._optional_id(
            payload.get("evidence_before_document_id"), "document_id", prefix="document"
        )
        title = _text(payload.get("title"), "issue_title", maximum=240)
        description = _text(
            payload.get("description", ""),
            "issue_description",
            required=False,
            maximum=8000,
        )
        severity = _text(
            payload.get("severity", "medium"), "issue_severity", maximum=16
        )
        if severity not in {"low", "medium", "high", "critical"}:
            raise ValueError("field_memory_issue_severity_invalid")
        assignee = (
            _text(
                payload.get("assignee", ""),
                "issue_assignee",
                required=False,
                maximum=160,
            )
            or None
        )
        due_at = _optional_timestamp(payload.get("due_at"), "issue_due_at")
        px, py, pz = self._optional_position(payload.get("position"))
        created_at = _now()
        with self._connection(write=True) as connection:
            inspection = connection.execute(
                "SELECT site_id, scene_version_id, status FROM inspections WHERE inspection_id = ?",
                (inspection_id,),
            ).fetchone()
            if inspection is None:
                raise FileNotFoundError(
                    f"field_memory_inspection_not_found:{inspection_id}"
                )
            if inspection["site_id"] != site_id:
                raise ValueError("field_memory_inspection_site_mismatch")
            if inspection["status"] != "in_progress":
                raise ValueError("field_memory_issue_requires_in_progress_inspection")
            scene_version_id = scene_version_id or inspection["scene_version_id"]
            if observation_id is not None:
                observation = connection.execute(
                    "SELECT inspection_id FROM inspection_observations WHERE inspection_observation_id = ?",
                    (observation_id,),
                ).fetchone()
                if observation is None or observation["inspection_id"] != inspection_id:
                    raise ValueError("field_memory_issue_observation_mismatch")
            self._require_site_link(
                connection,
                site_id=site_id,
                scene_version_id=scene_version_id,
                asset_id=asset_id,
                document_id=evidence_id,
            )
            initial_status = "assigned" if assignee else "open"
            connection.execute(
                "INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    issue_id,
                    inspection_id,
                    observation_id,
                    site_id,
                    asset_id,
                    scene_version_id,
                    title,
                    description,
                    severity,
                    initial_status,
                    assignee,
                    due_at,
                    px,
                    py,
                    pz,
                    evidence_id,
                    created_at,
                    created_at,
                ),
            )
            self._insert_issue_event(
                connection,
                issue_id,
                None,
                "created",
                None,
                initial_status,
                actor_id,
                "",
                {},
                created_at,
            )
        return self.issue(issue_id)

    @staticmethod
    def _insert_issue_event(
        connection: sqlite3.Connection,
        issue_id: str,
        action_id: str | None,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        actor_id: str,
        reason: str,
        details: dict[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO issue_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"issue-event_{uuid.uuid4().hex}",
                issue_id,
                action_id,
                event_type,
                from_status,
                to_status,
                actor_id,
                reason,
                _json_data(details, "issue_event_details"),
                created_at,
            ),
        )

    def issue(self, issue_id: str) -> dict[str, Any]:
        _identifier(issue_id, "issue_id", prefix="issue")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM issues WHERE issue_id = ?", (issue_id,)
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"field_memory_issue_not_found:{issue_id}")
            result = _row(row)
            result["corrective_actions"] = [
                _row(item)
                for item in connection.execute(
                    "SELECT * FROM corrective_actions WHERE issue_id = ? ORDER BY created_at",
                    (issue_id,),
                )
            ]
            result["events"] = [
                {**_row(item), "details": json.loads(item["details_json"])}
                for item in connection.execute(
                    "SELECT * FROM issue_events WHERE issue_id = ? ORDER BY created_at",
                    (issue_id,),
                )
            ]
            return result

    def transition_issue(
        self, issue_id: str, payload: dict[str, Any], *, actor_id: str
    ) -> dict[str, Any]:
        target = _text(payload.get("status"), "issue_status", maximum=32)
        reason = _text(payload.get("reason"), "issue_transition_reason", maximum=2000)
        assignee = (
            _text(
                payload.get("assignee", ""),
                "issue_assignee",
                required=False,
                maximum=160,
            )
            or None
        )
        transitions = {
            "open": {"assigned", "in_progress", "rejected"},
            "assigned": {"in_progress", "rejected"},
            "in_progress": {"resolved", "rejected"},
            "resolved": {"closed", "in_progress"},
            "closed": set(),
            "rejected": set(),
        }
        now = _now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM issues WHERE issue_id = ?", (issue_id,)
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"field_memory_issue_not_found:{issue_id}")
            current = str(row["status"])
            if target not in transitions[current]:
                raise ValueError(
                    f"field_memory_issue_transition_invalid:{current}->{target}"
                )
            if target == "assigned" and assignee is None:
                raise ValueError("field_memory_issue_assignee_required")
            if target == "resolved":
                completed = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM corrective_actions WHERE issue_id = ? AND status = 'completed'",
                        (issue_id,),
                    ).fetchone()[0]
                )
                if not completed:
                    raise ValueError(
                        "field_memory_issue_resolution_requires_completed_action"
                    )
            if target == "closed":
                approved = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM corrective_action_approvals AS approvals "
                        "JOIN corrective_actions AS actions USING(corrective_action_id) "
                        "WHERE actions.issue_id = ? AND actions.status = 'completed' AND approvals.decision = 'approved'",
                        (issue_id,),
                    ).fetchone()[0]
                )
                if not approved:
                    raise ValueError(
                        "field_memory_issue_close_requires_approved_action"
                    )
            next_assignee = assignee if target == "assigned" else row["assignee"]
            connection.execute(
                "UPDATE issues SET status = ?, assignee = ?, updated_at = ? WHERE issue_id = ?",
                (target, next_assignee, now, issue_id),
            )
            self._insert_issue_event(
                connection,
                issue_id,
                None,
                "status_transition",
                current,
                target,
                actor_id,
                reason,
                {"assignee": next_assignee},
                now,
            )
        return self.issue(issue_id)

    def corrective_actions(self, issue_id: str) -> list[dict[str, Any]]:
        return list(self.issue(issue_id)["corrective_actions"])

    def create_corrective_action(
        self, issue_id: str, payload: dict[str, Any], *, actor_id: str
    ) -> dict[str, Any]:
        issue = self.issue(issue_id)
        if issue["status"] in {"closed", "rejected"}:
            raise ValueError("field_memory_corrective_action_issue_terminal")
        action_id = _identifier(
            payload.get("corrective_action_id"),
            "corrective_action_id",
            prefix="corrective-action",
        )
        summary = _text(
            payload.get("summary"), "corrective_action_summary", maximum=4000
        )
        assigned_to = (
            _text(
                payload.get("assigned_to", ""),
                "corrective_action_assigned_to",
                required=False,
                maximum=160,
            )
            or None
        )
        due_at = _optional_timestamp(payload.get("due_at"), "corrective_action_due_at")
        status = "assigned" if assigned_to else "proposed"
        now = _now()
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO corrective_actions VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (action_id, issue_id, summary, status, assigned_to, due_at, now, now),
            )
            self._insert_issue_event(
                connection,
                issue_id,
                action_id,
                "corrective_action_created",
                None,
                status,
                actor_id,
                "",
                {"summary": summary},
                now,
            )
        return self.corrective_action(action_id)

    def corrective_action(self, action_id: str) -> dict[str, Any]:
        _identifier(action_id, "corrective_action_id", prefix="corrective-action")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM corrective_actions WHERE corrective_action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"field_memory_corrective_action_not_found:{action_id}"
                )
            result = _row(row)
            result["approvals"] = [
                _row(item)
                for item in connection.execute(
                    "SELECT * FROM corrective_action_approvals WHERE corrective_action_id = ? ORDER BY created_at",
                    (action_id,),
                )
            ]
            return result

    def transition_corrective_action(
        self, action_id: str, payload: dict[str, Any], *, actor_id: str
    ) -> dict[str, Any]:
        target = _text(payload.get("status"), "corrective_action_status", maximum=32)
        reason = _text(
            payload.get("reason"), "corrective_action_transition_reason", maximum=2000
        )
        assigned_to = (
            _text(
                payload.get("assigned_to", ""),
                "corrective_action_assigned_to",
                required=False,
                maximum=160,
            )
            or None
        )
        evidence_id = self._optional_id(
            payload.get("evidence_after_document_id"), "document_id", prefix="document"
        )
        transitions = {
            "proposed": {"assigned", "in_progress", "rejected"},
            "assigned": {"in_progress", "rejected"},
            "in_progress": {"completed", "rejected"},
            "completed": set(),
            "rejected": set(),
        }
        now = _now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT actions.*, issues.site_id, issues.asset_id FROM corrective_actions AS actions "
                "JOIN issues USING(issue_id) WHERE corrective_action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"field_memory_corrective_action_not_found:{action_id}"
                )
            current = str(row["status"])
            if target not in transitions[current]:
                raise ValueError(
                    f"field_memory_corrective_action_transition_invalid:{current}->{target}"
                )
            next_assignee = assigned_to or row["assigned_to"]
            if target == "assigned" and next_assignee is None:
                raise ValueError("field_memory_corrective_action_assignee_required")
            next_evidence = evidence_id or row["evidence_after_document_id"]
            if target == "completed" and next_evidence is None:
                raise ValueError(
                    "field_memory_corrective_action_completion_requires_evidence"
                )
            self._require_site_link(
                connection,
                site_id=row["site_id"],
                asset_id=row["asset_id"],
                document_id=next_evidence,
            )
            performed_at = now if target == "completed" else row["performed_at"]
            connection.execute(
                "UPDATE corrective_actions SET status = ?, assigned_to = ?, performed_at = ?, "
                "evidence_after_document_id = ?, updated_at = ? WHERE corrective_action_id = ?",
                (target, next_assignee, performed_at, next_evidence, now, action_id),
            )
            self._insert_issue_event(
                connection,
                row["issue_id"],
                action_id,
                "corrective_action_transition",
                current,
                target,
                actor_id,
                reason,
                {"evidence_after": next_evidence},
                now,
            )
        return self.corrective_action(action_id)

    def approve_corrective_action(
        self, action_id: str, payload: dict[str, Any], *, actor_id: str
    ) -> dict[str, Any]:
        decision = _text(
            payload.get("decision"), "corrective_action_approval_decision", maximum=16
        )
        if decision not in {"approved", "rejected"}:
            raise ValueError("field_memory_corrective_action_approval_decision_invalid")
        reason = _text(
            payload.get("reason"), "corrective_action_approval_reason", maximum=2000
        )
        now = _now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM corrective_actions WHERE corrective_action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"field_memory_corrective_action_not_found:{action_id}"
                )
            if row["status"] != "completed":
                raise ValueError(
                    "field_memory_corrective_action_approval_requires_completed"
                )
            connection.execute(
                "INSERT INTO corrective_action_approvals VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"approval_{uuid.uuid4().hex}",
                    action_id,
                    decision,
                    actor_id,
                    reason,
                    now,
                ),
            )
            self._insert_issue_event(
                connection,
                row["issue_id"],
                action_id,
                "approval",
                None,
                decision,
                actor_id,
                reason,
                {},
                now,
            )
        return self.corrective_action(action_id)

    def observations(self, asset_id: str) -> list[dict[str, Any]]:
        self._asset(asset_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM asset_observations WHERE asset_id = ? ORDER BY created_at DESC",
                (asset_id,),
            )
            return [self._observation(row) for row in rows]

    def create_observation(
        self, asset_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        asset = self._asset(asset_id)
        observation_id = _identifier(
            payload.get("observation_id"), "observation_id", prefix="observation"
        )
        scene_version_id = _identifier(
            _text(payload.get("scene_version_id"), "scene_version_id", maximum=96),
            "scene_version_id",
            prefix="scene",
        )
        px, py, pz, qx, qy, qz, qw = _pose(payload.get("pose"))
        source = _text(
            payload.get("source", "manual"), "observation_source", maximum=64
        )
        created_at = _now()
        with self._connection(write=True) as connection:
            scene = connection.execute(
                "SELECT site_id FROM scene_versions WHERE scene_version_id = ?",
                (scene_version_id,),
            ).fetchone()
            if scene is None:
                raise FileNotFoundError(
                    f"field_memory_scene_version_not_found:{scene_version_id}"
                )
            if scene["site_id"] != asset["site_id"]:
                raise ValueError("field_memory_observation_site_mismatch")
            connection.execute(
                "INSERT INTO asset_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation_id,
                    asset_id,
                    scene_version_id,
                    px,
                    py,
                    pz,
                    qx,
                    qy,
                    qz,
                    qw,
                    source,
                    created_at,
                ),
            )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM asset_observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
            assert row is not None
            return self._observation(row)

    def documents(self, asset_id: str) -> list[dict[str, Any]]:
        self._asset(asset_id)
        with self._connection() as connection:
            return [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM asset_documents WHERE asset_id = ? ORDER BY created_at DESC",
                    (asset_id,),
                )
            ]

    def maintenance_records(self, asset_id: str) -> list[dict[str, Any]]:
        self._asset(asset_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM maintenance_records WHERE asset_id = ? ORDER BY performed_at DESC, created_at DESC",
                (asset_id,),
            )
            return [self._maintenance_record(row) for row in rows]

    def create_maintenance_record(
        self, asset_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._asset(asset_id)
        record_id = _identifier(
            payload.get("maintenance_record_id"),
            "maintenance_record_id",
            prefix="maintenance",
        )
        performed_at = _required_timestamp(
            payload.get("performed_at"), "maintenance_performed_at"
        )
        status = _text(
            payload.get("status", "completed"), "maintenance_status", maximum=64
        )
        summary = _text(payload.get("summary"), "maintenance_summary", maximum=1000)
        details_json = _json_data(payload.get("details", {}), "maintenance_details")
        created_at = _now()
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO maintenance_records(maintenance_record_id, asset_id, performed_at, status, "
                "summary, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    asset_id,
                    performed_at,
                    status,
                    summary,
                    details_json,
                    created_at,
                ),
            )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM maintenance_records WHERE maintenance_record_id = ?",
                (record_id,),
            ).fetchone()
            assert row is not None
            return self._maintenance_record(row)

    def create_document(self, asset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._asset(asset_id)
        document_id = _identifier(
            payload.get("document_id"), "document_id", prefix="document"
        )
        kind = _text(payload.get("kind", "manual"), "document_kind", maximum=64)
        title = _text(payload.get("title"), "document_title", maximum=200)
        uri = _text(payload.get("uri"), "document_uri", maximum=2048)
        content_sha256 = payload.get("content_sha256")
        if content_sha256 not in (None, "") and (
            not isinstance(content_sha256, str)
            or SHA256_RE.fullmatch(content_sha256) is None
        ):
            raise ValueError("field_memory_document_content_sha256_invalid")
        if kind in {"image", "pdf"}:
            if not isinstance(content_sha256, str):
                raise ValueError("field_memory_image_managed_content_required")
            content_sha256 = content_sha256.lower()
            if uri != f"content://sha256/{content_sha256}":
                raise ValueError("field_memory_image_managed_content_required")
            self.resolve_document_content(
                {"uri": uri, "content_sha256": content_sha256}
            )
        memo = _text(
            payload.get("memo", ""), "document_memo", required=False, maximum=4000
        )
        created_at = _now()
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO asset_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    asset_id,
                    kind,
                    title,
                    uri,
                    content_sha256 or None,
                    memo,
                    created_at,
                ),
            )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM asset_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            assert row is not None
            return _row(row)

    def store_asset_image(
        self,
        asset_id: str,
        filename: str,
        stream: BinaryIO,
        content_length: int,
        *,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Store one immutable image and register it as asset evidence.

        Image bytes are canonical business evidence.  Derived thumbnails,
        vectors, and FAISS files are deliberately stored elsewhere.
        """

        self._asset(asset_id)
        if content_length <= 0 or content_length > MAX_STORED_IMAGE_BYTES:
            raise ValueError("field_memory_image_size_invalid")
        safe_name = Path(filename).name
        if not safe_name or safe_name != filename or len(safe_name) > 255:
            raise ValueError("field_memory_image_filename_invalid")
        display_title = _text(title or safe_name, "document_title", maximum=200)
        staging = self.staging_root
        staging.mkdir(parents=True, exist_ok=True)
        temporary = staging / f"image-{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        total = 0
        try:
            with temporary.open("xb") as output:
                while True:
                    block = stream.read(min(1024 * 1024, content_length - total))
                    if not block:
                        break
                    total += len(block)
                    if total > content_length or total > MAX_STORED_IMAGE_BYTES:
                        raise ValueError("field_memory_image_size_invalid")
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if total != content_length:
                raise ValueError("field_memory_image_body_incomplete")
            try:
                with Image.open(temporary) as image:
                    image.verify()
                    image_format = str(image.format or "").upper()
            except (OSError, ValueError) as error:
                raise ValueError("field_memory_image_invalid") from error
            if image_format not in STORED_IMAGE_FORMATS:
                raise ValueError(
                    f"field_memory_image_format_unsupported:{image_format or 'unknown'}"
                )

            content_sha256 = digest.hexdigest()
            destination = self.content_path(content_sha256)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temporary.unlink()
            else:
                os.replace(temporary, destination)
            document = self.create_document(
                asset_id,
                {
                    "kind": "image",
                    "title": display_title,
                    "uri": f"content://sha256/{content_sha256}",
                    "content_sha256": content_sha256,
                },
            )
            document["content_length"] = content_length
            document["image_format"] = image_format
            return document
        finally:
            temporary.unlink(missing_ok=True)

    def store_asset_document(
        self,
        asset_id: str,
        filename: str,
        stream: BinaryIO,
        content_length: int,
        *,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Store a PDF or supported image as immutable managed evidence."""

        self._asset(asset_id)
        if content_length <= 0 or content_length > MAX_STORED_DOCUMENT_BYTES:
            raise ValueError("field_memory_document_size_invalid")
        safe_name = Path(filename).name
        if not safe_name or safe_name != filename or len(safe_name) > 255:
            raise ValueError("field_memory_document_filename_invalid")
        display_title = _text(title or safe_name, "document_title", maximum=200)
        staging = self.staging_root
        staging.mkdir(parents=True, exist_ok=True)
        temporary = staging / f"document-{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        total = 0
        try:
            with temporary.open("xb") as output:
                while True:
                    block = stream.read(min(1024 * 1024, content_length - total))
                    if not block:
                        break
                    total += len(block)
                    if total > content_length or total > MAX_STORED_DOCUMENT_BYTES:
                        raise ValueError("field_memory_document_size_invalid")
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if total != content_length:
                raise ValueError("field_memory_document_body_incomplete")
            with temporary.open("rb") as input_stream:
                prefix = input_stream.read(16)
            suffix = safe_name.lower()
            if prefix.startswith(b"%PDF-") and suffix.endswith(".pdf"):
                kind = "pdf"
            else:
                try:
                    with Image.open(temporary) as image:
                        image.verify()
                        image_format = str(image.format or "").upper()
                except (OSError, ValueError) as error:
                    raise ValueError(
                        "field_memory_document_format_unsupported"
                    ) from error
                if image_format not in STORED_IMAGE_FORMATS:
                    raise ValueError("field_memory_document_format_unsupported")
                kind = "image"
            content_sha256 = digest.hexdigest()
            destination = self.content_path(content_sha256)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temporary.unlink()
            else:
                os.replace(temporary, destination)
            document = self.create_document(
                asset_id,
                {
                    "kind": kind,
                    "title": display_title,
                    "uri": f"content://sha256/{content_sha256}",
                    "content_sha256": content_sha256,
                },
            )
            document.update({"content_length": content_length, "media_kind": kind})
            return document
        finally:
            temporary.unlink(missing_ok=True)

    def append_audit_event(
        self,
        *,
        actor_id: str,
        role: str,
        site_id: str | None,
        action: str,
        target: str,
        request_id: str | None,
        result: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_id = f"audit_{uuid.uuid4().hex}"
        created_at = _now()
        details_json = _json_data(details or {}, "audit_details", maximum=16_000)
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    actor_id,
                    role,
                    site_id,
                    action,
                    target,
                    request_id,
                    result,
                    details_json,
                    created_at,
                ),
            )
        return {"audit_event_id": event_id, "created_at": created_at}

    def audit_events(
        self, site_id: str | None = None, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("field_memory_audit_limit_invalid")
        with self._connection() as connection:
            if site_id:
                self.site(site_id)
                rows = connection.execute(
                    "SELECT * FROM audit_events WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (site_id, limit),
                )
            else:
                rows = connection.execute(
                    "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            return [
                {**_row(row), "details": json.loads(row["details_json"])}
                for row in rows
            ]

    def generate_field_operations_report(self, site_id: str) -> dict[str, Any]:
        """Materialize a provenance-bearing PDF, CSV, and manifest."""

        from .field_reports import generate_field_operations_report

        return generate_field_operations_report(self, site_id)

    def content_path(self, content_sha256: str) -> Path:
        if (
            not isinstance(content_sha256, str)
            or SHA256_RE.fullmatch(content_sha256) is None
        ):
            raise ValueError("field_memory_document_content_sha256_invalid")
        return self.attachments_root / content_sha256[:2] / content_sha256.lower()

    def frame_content_site_ids(self, content_sha256: str) -> list[str]:
        """Return the Sites that own a managed image used by frame analysis."""

        if (
            not isinstance(content_sha256, str)
            or SHA256_RE.fullmatch(content_sha256) is None
        ):
            raise ValueError("field_memory_document_content_sha256_invalid")
        digest = content_sha256.lower()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT site_id FROM frame_analyses "
                "WHERE image_content_sha256 = ? OR vlm_input_content_sha256 = ? "
                "OR thumbnail_content_sha256 = ? ORDER BY site_id",
                (digest, digest, digest),
            )
            return [str(row["site_id"]) for row in rows]

    def store_managed_image_bytes(self, content: bytes) -> dict[str, Any]:
        """Store immutable image bytes without creating an asset document."""

        if not content or len(content) > MAX_STORED_IMAGE_BYTES:
            raise ValueError("field_memory_image_size_invalid")
        try:
            from io import BytesIO

            with Image.open(BytesIO(content)) as image:
                image.verify()
                image_format = str(image.format or "").upper()
        except (OSError, ValueError) as error:
            raise ValueError("field_memory_image_invalid") from error
        if image_format not in STORED_IMAGE_FORMATS:
            raise ValueError(
                f"field_memory_image_format_unsupported:{image_format or 'unknown'}"
            )
        content_sha256 = hashlib.sha256(content).hexdigest()
        destination = self.content_path(content_sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_name(
                f".{destination.name}.{uuid.uuid4().hex}.part"
            )
            try:
                with temporary.open("xb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "content_sha256": content_sha256,
            "content_length": len(content),
            "image_format": image_format,
        }

    def resolve_document_content(self, document: dict[str, Any]) -> Path:
        content_sha256 = document.get("content_sha256")
        uri = document.get("uri")
        expected_uri = f"content://sha256/{content_sha256}"
        if not isinstance(content_sha256, str) or uri != expected_uri:
            raise ValueError("field_memory_document_not_managed_content")
        path = self.content_path(content_sha256)
        if not path.is_file():
            raise FileNotFoundError(
                f"field_memory_document_content_missing:{content_sha256}"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != content_sha256.lower():
            raise ValueError(
                f"field_memory_document_content_hash_mismatch:{content_sha256}"
            )
        return path

    def _asset(self, asset_id: str) -> dict[str, Any]:
        _identifier(asset_id, "asset_id", prefix="asset")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"field_memory_asset_not_found:{asset_id}")
            return _row(row)

    @staticmethod
    def _observation(row: sqlite3.Row) -> dict[str, Any]:
        value = _row(row)
        return {
            "observation_id": value["observation_id"],
            "asset_id": value["asset_id"],
            "scene_version_id": value["scene_version_id"],
            "pose": {
                "position": {
                    "x": value["position_x"],
                    "y": value["position_y"],
                    "z": value["position_z"],
                },
                "rotation": {
                    "x": value["rotation_x"],
                    "y": value["rotation_y"],
                    "z": value["rotation_z"],
                    "w": value["rotation_w"],
                },
            },
            "source": value["source"],
            "created_at": value["created_at"],
        }

    @staticmethod
    def _maintenance_record(row: sqlite3.Row) -> dict[str, Any]:
        value = _row(row)
        value["details"] = json.loads(value.pop("details_json"))
        return value


def dispatch_field_memory_api(
    service: FieldMemoryService,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    *,
    enabled_features: frozenset[str],
    actor_id: str = "local-operator",
) -> Any:
    """Dispatch the closed field-memory route family."""

    require_feature(enabled_features, "field-memory")
    body = payload or {}
    if method == "GET" and path == "/api/field-memory/summary":
        return service.summary()
    if method == "GET" and path == "/api/field-memory/verified-scene-packages":
        return service.verified_scene_packages()
    if path == "/api/field-memory/sites":
        if method == "GET":
            return service.sites()
        if method == "POST":
            return service.create_site(body)
    if method == "GET" and path == "/api/field-memory/audit-events":
        return service.audit_events()
    site_match = re.fullmatch(
        r"/api/field-memory/sites/([^/]+)(?:/(scene-versions|assets|captures|inspections|issues))?",
        path,
    )
    if site_match:
        site_id, child = site_match.groups()
        if child is None and method == "GET":
            return service.site(site_id)
        if child == "scene-versions":
            return (
                service.scene_versions(site_id)
                if method == "GET"
                else service.create_scene_version(site_id, body)
            )
        if child == "assets":
            return (
                service.assets(site_id)
                if method == "GET"
                else service.create_asset(site_id, body)
            )
        if child == "captures":
            return (
                service.captures(site_id)
                if method == "GET"
                else service.create_capture(site_id, body)
            )
        if child == "inspections":
            return (
                service.inspections(site_id)
                if method == "GET"
                else service.create_inspection(site_id, body)
            )
        if child == "issues":
            return (
                service.issues(site_id)
                if method == "GET"
                else service.create_issue(site_id, body, actor_id=actor_id)
            )
    report_match = re.fullmatch(r"/api/field-memory/sites/([^/]+)/reports", path)
    if report_match and method == "POST":
        return service.generate_field_operations_report(report_match.group(1))
    inspection_match = re.fullmatch(
        r"/api/field-memory/inspections/([^/]+)(?:/(transition|observations))?", path
    )
    if inspection_match:
        inspection_id, child = inspection_match.groups()
        if child is None and method == "GET":
            return service.inspection(inspection_id)
        if child == "transition" and method == "POST":
            return service.transition_inspection(inspection_id, body, actor_id=actor_id)
        if child == "observations" and method == "POST":
            return service.create_inspection_observation(inspection_id, body)
    issue_match = re.fullmatch(
        r"/api/field-memory/issues/([^/]+)(?:/(transition|actions))?", path
    )
    if issue_match:
        issue_id, child = issue_match.groups()
        if child is None and method == "GET":
            return service.issue(issue_id)
        if child == "transition" and method == "POST":
            return service.transition_issue(issue_id, body, actor_id=actor_id)
        if child == "actions":
            return (
                service.corrective_actions(issue_id)
                if method == "GET"
                else service.create_corrective_action(issue_id, body, actor_id=actor_id)
            )
    action_match = re.fullmatch(
        r"/api/field-memory/corrective-actions/([^/]+)/(transition|approvals)", path
    )
    if action_match and method == "POST":
        action_id, child = action_match.groups()
        if child == "transition":
            return service.transition_corrective_action(
                action_id, body, actor_id=actor_id
            )
        return service.approve_corrective_action(action_id, body, actor_id=actor_id)
    asset_match = re.fullmatch(
        r"/api/field-memory/assets/([^/]+)/(observations|documents|maintenance-records)",
        path,
    )
    if asset_match:
        asset_id, child = asset_match.groups()
        if child == "observations":
            return (
                service.observations(asset_id)
                if method == "GET"
                else service.create_observation(asset_id, body)
            )
        if child == "documents":
            return (
                service.documents(asset_id)
                if method == "GET"
                else service.create_document(asset_id, body)
            )
        return (
            service.maintenance_records(asset_id)
            if method == "GET"
            else service.create_maintenance_record(asset_id, body)
        )
    raise FileNotFoundError(f"api_endpoint_not_found:{method}:{path}")
