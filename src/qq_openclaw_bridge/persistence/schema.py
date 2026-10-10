import hashlib
import sqlite3


SCHEMA_VERSION = "7"
ROUTING_CONFIG_HASH_MIGRATION_MARKER = "routing_config_hash_compat_v1"
_ROUTING_CONFIG_HASH_MIGRATION_COMPLETE = "1"


def migrate_to_v7(connection: sqlite3.Connection) -> None:
    """Add derived community-style state inside the initialization transaction."""
    schema_sql = """
    CREATE TABLE IF NOT EXISTS community_style_sources (
      id TEXT PRIMARY KEY,
      platform TEXT NOT NULL CHECK(platform IN ('reddit','tieba')),
      community_key TEXT NOT NULL,
      profile_key TEXT NOT NULL,
      enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
      schedule_seconds INTEGER NOT NULL CHECK(schedule_seconds >= 300),
      max_items_per_sync INTEGER NOT NULL CHECK(max_items_per_sync BETWEEN 1 AND 100),
      model TEXT NOT NULL,
      cursor_json TEXT NOT NULL DEFAULT '{}',
      last_attempt_at TEXT, last_success_at TEXT,
      status TEXT NOT NULL CHECK(status IN ('idle','running','retry','paused')),
      safe_error_class TEXT, next_attempt_at TEXT NOT NULL,
      config_fingerprint TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS community_style_sources_due
      ON community_style_sources(enabled, status, next_attempt_at, id);
    CREATE TABLE IF NOT EXISTS style_authorizations (
      id TEXT PRIMARY KEY,
      source_id TEXT NOT NULL REFERENCES community_style_sources(id),
      subject_type TEXT NOT NULL CHECK(subject_type IN ('author','post')),
      subject_key TEXT NOT NULL,
      scope TEXT NOT NULL CHECK(scope IN ('single_post','all_posts_until_revoke')),
      evidence_ref TEXT NOT NULL, granted_at TEXT NOT NULL, expires_at TEXT,
      status TEXT NOT NULL CHECK(status IN ('active','revoked','expired')),
      revoked_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS style_authorizations_one_current
      ON style_authorizations(source_id, subject_type, subject_key) WHERE status='active';
    CREATE INDEX IF NOT EXISTS style_authorizations_source
      ON style_authorizations(source_id, status, expires_at);
    CREATE TABLE IF NOT EXISTS community_style_samples (
      id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT NOT NULL,
      source_id TEXT NOT NULL REFERENCES community_style_sources(id),
      profile_key TEXT NOT NULL,
      authorization_id TEXT NOT NULL REFERENCES style_authorizations(id),
      external_content_id TEXT NOT NULL, canonical_url_hash TEXT NOT NULL,
      author_key_hash TEXT NOT NULL, content_sha256 TEXT NOT NULL,
      feature_schema_version INTEGER NOT NULL CHECK(feature_schema_version=1),
      features_json TEXT NOT NULL, model TEXT NOT NULL,
      analyzed_at TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('active','removed')), removed_at TEXT,
      UNIQUE(source_id, external_content_id, content_sha256)
    );
    CREATE INDEX IF NOT EXISTS community_style_samples_profile
      ON community_style_samples(profile_key, status, id);
    CREATE INDEX IF NOT EXISTS community_style_samples_authorization
      ON community_style_samples(authorization_id, status);
    CREATE TABLE IF NOT EXISTS community_style_profiles (
      id INTEGER PRIMARY KEY AUTOINCREMENT, profile_key TEXT NOT NULL,
      version INTEGER NOT NULL CHECK(version > 0),
      status TEXT NOT NULL CHECK(status IN ('candidate','active','retired')),
      rules_json TEXT NOT NULL, sample_count INTEGER NOT NULL CHECK(sample_count >= 0),
      author_count INTEGER NOT NULL CHECK(author_count >= 0), source_fingerprint TEXT NOT NULL,
      created_at TEXT NOT NULL, activated_at TEXT, invalidated_at TEXT,
      UNIQUE(profile_key, version)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS community_style_one_active_profile
      ON community_style_profiles(profile_key) WHERE status='active';
    CREATE TABLE IF NOT EXISTS group_style_bindings (
      group_id INTEGER PRIMARY KEY CHECK(group_id > 0), profile_key TEXT NOT NULL,
      profile_version INTEGER CHECK(profile_version > 0),
      enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
      cooldown_seconds INTEGER NOT NULL CHECK(cooldown_seconds > 0),
      max_reply_chars INTEGER NOT NULL CHECK(max_reply_chars > 0),
      config_fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS group_message_receipts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
      group_id INTEGER NOT NULL CHECK(group_id > 0), inbound_message_sha256 TEXT NOT NULL,
      outbound_message_id TEXT,
      status TEXT NOT NULL CHECK(status IN ('processing','sent','failed')),
      attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
      next_attempt_at TEXT NOT NULL, safe_error_class TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sent_at TEXT,
      UNIQUE(group_id, inbound_message_sha256)
    );
    CREATE INDEX IF NOT EXISTS group_message_receipts_outbound
      ON group_message_receipts(group_id, outbound_message_id) WHERE status='sent';
    """
    for statement in schema_sql.split(";"):
        if statement.strip():
            connection.execute(statement)


def migrate_to_v5(connection: sqlite3.Connection) -> None:
    """Apply the F0 schema additions in the caller's transaction."""

    statements = (
        """
        CREATE TABLE IF NOT EXISTS orchestration_runs (
          run_id TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          source_message_id TEXT,
          session_key TEXT NOT NULL,
          mode TEXT NOT NULL,
          risk_level TEXT NOT NULL,
          status TEXT NOT NULL,
          config_version INTEGER NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          fallback_reason TEXT,
          safe_error_class TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS orchestration_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          stage TEXT NOT NULL,
          detail TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES orchestration_runs(run_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS orchestration_config_versions (
          version INTEGER PRIMARY KEY AUTOINCREMENT,
          config_json TEXT NOT NULL,
          config_sha256 TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          activated_at TEXT
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS orchestration_one_active_config
          ON orchestration_config_versions(status) WHERE status='active'
        """,
        """
        CREATE INDEX IF NOT EXISTS orchestration_runs_user_started
          ON orchestration_runs(user_id, started_at, run_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS orchestration_runs_status_started
          ON orchestration_runs(status, started_at, run_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS outbox_final_reconciliation
          ON outbox(user_id, dedupe_key)
          WHERE status='sent' AND kind IN ('reply','task_final')
        """,
        """
        CREATE INDEX IF NOT EXISTS orchestration_events_run_id
          ON orchestration_events(run_id, id)
        """,
        """
        CREATE TABLE IF NOT EXISTS action_intents (
          intent_id TEXT PRIMARY KEY,
          approval_id TEXT NOT NULL UNIQUE,
          user_id INTEGER NOT NULL,
          source_message_id TEXT NOT NULL,
          session_key TEXT NOT NULL,
          action_type TEXT NOT NULL,
          adapter_id TEXT NOT NULL,
          schema_version INTEGER NOT NULL,
          payload_json TEXT NOT NULL,
          payload_sha256 TEXT NOT NULL,
          config_version INTEGER NOT NULL,
          risk_tier TEXT NOT NULL,
          impact_summary TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          approved_at TEXT,
          attempt INTEGER NOT NULL DEFAULT 0,
          result_class TEXT,
          safe_result_summary TEXT,
          finished_at TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS action_intents_owner_status
          ON action_intents(user_id, status, expires_at)
        """,
    )
    for statement in statements:
        connection.execute(statement)


def migrate_to_v6(connection: sqlite3.Connection) -> None:
    """Apply the F1 shadow-routing schema additions in the caller's transaction."""

    statements = (
        """
        CREATE TABLE IF NOT EXISTS routing_config_versions (
          version INTEGER PRIMARY KEY AUTOINCREMENT,
          config_json TEXT NOT NULL,
          config_sha256 TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          activated_at TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS routing_config_sha256
          ON routing_config_versions(config_sha256)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS routing_one_active_config
          ON routing_config_versions(status) WHERE status='active'
        """,
        """
        CREATE TABLE IF NOT EXISTS route_decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          source_message_sha256 TEXT NOT NULL,
          message_sha256 TEXT NOT NULL,
          message_preview TEXT NOT NULL,
          routing_config_version INTEGER NOT NULL,
          rule_version TEXT NOT NULL,
          dispatch_overhead_ms INTEGER NOT NULL,
          status TEXT NOT NULL,
          rule_level TEXT,
          model_level TEXT,
          final_level TEXT,
          confidence REAL,
          task_kind TEXT,
          risk_flags_json TEXT NOT NULL,
          reasons_json TEXT NOT NULL,
          brainstorm_required INTEGER NOT NULL DEFAULT 0,
          brainstorm_mode TEXT,
          classifier_provider TEXT,
          classifier_model TEXT,
          classifier_duration_ms INTEGER,
          safe_error_class TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          finished_at TEXT,
          classifier_attempted INTEGER NOT NULL DEFAULT 0,
          classifier_attempted_at TEXT,
          UNIQUE(user_id, source_message_sha256, routing_config_version)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS route_decisions_owner_created
          ON route_decisions(user_id, created_at, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS route_decisions_status_created
          ON route_decisions(status, created_at, id)
        """,
        """
        CREATE TABLE IF NOT EXISTS route_labels (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          decision_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL,
          verdict TEXT NOT NULL,
          correct_level TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(decision_id, user_id),
          FOREIGN KEY(decision_id) REFERENCES route_decisions(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS route_labels_decision_owner
          ON route_labels(decision_id, user_id, id)
        """,
        """
        CREATE TABLE IF NOT EXISTS routing_classifier_circuit (
          config_version INTEGER PRIMARY KEY,
          state TEXT NOT NULL,
          opened_at TEXT,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(config_version) REFERENCES routing_config_versions(version)
        )
        """,
    )
    migrate_routing_hashes = not _routing_config_hash_migration_complete(
        connection
    )
    if migrate_routing_hashes:
        _migrate_routing_config_hash_uniqueness(connection)
    for statement in statements:
        connection.execute(statement)
    decision_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(route_decisions)")
    }
    if "classifier_attempted" not in decision_columns:
        connection.execute(
            "ALTER TABLE route_decisions "
            "ADD COLUMN classifier_attempted INTEGER NOT NULL DEFAULT 0"
        )
    if "classifier_attempted_at" not in decision_columns:
        connection.execute(
            "ALTER TABLE route_decisions ADD COLUMN classifier_attempted_at TEXT"
        )
        connection.execute(
            "UPDATE route_decisions SET classifier_attempted_at=created_at "
            "WHERE classifier_attempted=1"
        )
    # v6 originally kept the classifier fuse only in memory. Its restart
    # behavior was therefore closed; preserve that explicit baseline for
    # every existing config while keeping this migration idempotent.
    connection.execute(
        "INSERT OR IGNORE INTO routing_classifier_circuit("
        "config_version, state, opened_at, updated_at) "
        "SELECT version, 'closed', NULL, COALESCE(activated_at, created_at) "
        "FROM routing_config_versions"
    )
    if migrate_routing_hashes:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                ROUTING_CONFIG_HASH_MIGRATION_MARKER,
                _ROUTING_CONFIG_HASH_MIGRATION_COMPLETE,
            ),
        )


def _routing_config_hash_migration_complete(
    connection: sqlite3.Connection,
) -> bool:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key=?",
        (ROUTING_CONFIG_HASH_MIGRATION_MARKER,),
    ).fetchone()
    return row == (_ROUTING_CONFIG_HASH_MIGRATION_COMPLETE,)


def _migrate_routing_config_hash_uniqueness(connection: sqlite3.Connection) -> None:
    """One-time repair of legacy v6 digests and hash index uniqueness."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='routing_config_versions'"
    ).fetchone()
    if exists is None:
        return
    if _routing_config_hash_is_unique(connection):
        connection.execute(
            """
            CREATE TABLE routing_config_versions_replacement (
              version INTEGER PRIMARY KEY AUTOINCREMENT,
              config_json TEXT NOT NULL,
              config_sha256 TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              activated_at TEXT
            )
            """
        )
        rows = connection.execute(
            "SELECT version, config_json, status, created_at, activated_at "
            "FROM routing_config_versions ORDER BY version"
        ).fetchall()
        connection.executemany(
            "INSERT INTO routing_config_versions_replacement("
            "version, config_json, config_sha256, status, created_at, activated_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    version,
                    config_json,
                    hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
                    status,
                    created_at,
                    activated_at,
                )
                for version, config_json, status, created_at, activated_at in rows
            ),
        )
        connection.execute("DROP TABLE routing_config_versions")
        connection.execute(
            "ALTER TABLE routing_config_versions_replacement "
            "RENAME TO routing_config_versions"
        )
    _rehash_routing_config_versions(connection)


def _rehash_routing_config_versions(connection: sqlite3.Connection) -> None:
    updates = []
    for version, config_json, stored_hash in connection.execute(
        "SELECT version, config_json, config_sha256 FROM routing_config_versions"
    ):
        expected_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
        if stored_hash != expected_hash:
            updates.append((expected_hash, version))
    if updates:
        connection.executemany(
            "UPDATE routing_config_versions SET config_sha256=? WHERE version=?",
            updates,
        )


def _routing_config_hash_is_unique(connection: sqlite3.Connection) -> bool:
    for row in connection.execute("PRAGMA index_list(routing_config_versions)"):
        if not bool(row[2]):
            continue
        columns = [
            index_row[2]
            for index_row in connection.execute("PRAGMA index_info(%s)" % row[1])
        ]
        if columns == ["config_sha256"]:
            return True
    return False


__all__ = [
    "ROUTING_CONFIG_HASH_MIGRATION_MARKER",
    "SCHEMA_VERSION",
    "migrate_to_v5",
    "migrate_to_v6",
    "migrate_to_v7",
]
