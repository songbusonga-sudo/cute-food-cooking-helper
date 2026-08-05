"""Mirror online user data into local MySQL backup tables.

Local MYSQL_* settings point to the local database.
ONLINE_MYSQL_* settings point to the online production database.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import mysql.connector


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


MIRROR_TABLE_SQL = [
    """
    CREATE TABLE IF NOT EXISTS online_user_sync_runs (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
      started_at DATETIME NOT NULL,
      finished_at DATETIME NULL,
      source_host VARCHAR(255) NOT NULL,
      source_database VARCHAR(128) NOT NULL,
      status VARCHAR(20) NOT NULL,
      summary JSON NULL,
      error_message TEXT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS online_users (
      source_user_id BIGINT UNSIGNED PRIMARY KEY,
      external_key VARCHAR(64) NOT NULL,
      display_name VARCHAR(80) NOT NULL DEFAULT '小锅用户',
      password_hash VARCHAR(255) NULL,
      role VARCHAR(20) NOT NULL DEFAULT 'user',
      is_active BOOLEAN NOT NULL DEFAULT TRUE,
      taste_settings JSON NULL,
      utensil_settings JSON NULL,
      created_at DATETIME NULL,
      updated_at DATETIME NULL,
      last_synced_at DATETIME NOT NULL,
      last_seen_run_id BIGINT UNSIGNED NOT NULL,
      UNIQUE KEY uq_online_users_external_key (external_key),
      INDEX idx_online_users_last_seen (last_seen_run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS online_user_selected_ingredients (
      source_user_id BIGINT UNSIGNED NOT NULL,
      ingredient_id VARCHAR(40) NOT NULL,
      selected_at DATETIME NULL,
      last_synced_at DATETIME NOT NULL,
      last_seen_run_id BIGINT UNSIGNED NOT NULL,
      PRIMARY KEY (source_user_id, ingredient_id),
      INDEX idx_online_selected_last_seen (last_seen_run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS online_user_favorites (
      source_user_id BIGINT UNSIGNED NOT NULL,
      recipe_id VARCHAR(60) NOT NULL,
      created_at DATETIME NULL,
      last_synced_at DATETIME NOT NULL,
      last_seen_run_id BIGINT UNSIGNED NOT NULL,
      PRIMARY KEY (source_user_id, recipe_id),
      INDEX idx_online_favorites_last_seen (last_seen_run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS online_user_auth_events (
      source_event_id BIGINT UNSIGNED PRIMARY KEY,
      source_user_id BIGINT UNSIGNED NOT NULL,
      event_type VARCHAR(20) NOT NULL,
      created_at DATETIME NULL,
      last_synced_at DATETIME NOT NULL,
      last_seen_run_id BIGINT UNSIGNED NOT NULL,
      INDEX idx_online_auth_user_created (source_user_id, created_at),
      INDEX idx_online_auth_last_seen (last_seen_run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS online_cooking_records (
      source_record_id BIGINT UNSIGNED PRIMARY KEY,
      source_user_id BIGINT UNSIGNED NOT NULL,
      recipe_id VARCHAR(60) NOT NULL,
      rating TINYINT UNSIGNED NULL,
      notes VARCHAR(500) NULL,
      finished_at DATETIME NULL,
      last_synced_at DATETIME NOT NULL,
      last_seen_run_id BIGINT UNSIGNED NOT NULL,
      INDEX idx_online_records_user_finished (source_user_id, finished_at),
      INDEX idx_online_records_last_seen (last_seen_run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


TABLES = [
    {
        "source": "users",
        "target": "online_users",
        "columns": [
            ("id", "source_user_id", True),
            ("external_key", "external_key", True),
            ("display_name", "display_name", True),
            ("password_hash", "password_hash", False),
            ("role", "role", False),
            ("is_active", "is_active", False),
            ("taste_settings", "taste_settings", False),
            ("utensil_settings", "utensil_settings", False),
            ("created_at", "created_at", False),
            ("updated_at", "updated_at", False),
        ],
        "json_columns": {"taste_settings", "utensil_settings"},
        "primary_key": ["source_user_id"],
    },
    {
        "source": "user_selected_ingredients",
        "target": "online_user_selected_ingredients",
        "columns": [
            ("user_id", "source_user_id", True),
            ("ingredient_id", "ingredient_id", True),
            ("selected_at", "selected_at", False),
        ],
        "json_columns": set(),
        "primary_key": ["source_user_id", "ingredient_id"],
    },
    {
        "source": "user_favorites",
        "target": "online_user_favorites",
        "columns": [
            ("user_id", "source_user_id", True),
            ("recipe_id", "recipe_id", True),
            ("created_at", "created_at", False),
        ],
        "json_columns": set(),
        "primary_key": ["source_user_id", "recipe_id"],
    },
    {
        "source": "user_auth_events",
        "target": "online_user_auth_events",
        "columns": [
            ("id", "source_event_id", True),
            ("user_id", "source_user_id", True),
            ("event_type", "event_type", True),
            ("created_at", "created_at", False),
        ],
        "json_columns": set(),
        "primary_key": ["source_event_id"],
    },
    {
        "source": "cooking_records",
        "target": "online_cooking_records",
        "columns": [
            ("id", "source_record_id", True),
            ("user_id", "source_user_id", True),
            ("recipe_id", "recipe_id", True),
            ("rating", "rating", False),
            ("notes", "notes", False),
            ("finished_at", "finished_at", False),
        ],
        "json_columns": set(),
        "primary_key": ["source_record_id"],
    },
]


def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def env_value(env: dict[str, str], *keys: str, default: str | None = None) -> str | None:
    for key in keys:
        if os.getenv(key):
            return os.getenv(key)
        if env.get(key):
            return env[key]
    return default


def database_config(env: dict[str, str], prefix: str) -> dict[str, object]:
    if prefix:
        host = env_value(env, f"{prefix}MYSQL_HOST", f"{prefix}MYSQLHOST")
        port = env_value(env, f"{prefix}MYSQL_PORT", f"{prefix}MYSQLPORT", default="3306")
        user = env_value(env, f"{prefix}MYSQL_USER", f"{prefix}MYSQLUSER")
        password = env_value(env, f"{prefix}MYSQL_PASSWORD", f"{prefix}MYSQLPASSWORD", default="")
        database = env_value(env, f"{prefix}MYSQL_DATABASE", f"{prefix}MYSQLDATABASE")
    else:
        host = env_value(env, "MYSQL_HOST", "MYSQLHOST", default="127.0.0.1")
        port = env_value(env, "MYSQL_PORT", "MYSQLPORT", default="3306")
        user = env_value(env, "MYSQL_USER", "MYSQLUSER", default="food_app")
        password = env_value(env, "MYSQL_PASSWORD", "MYSQLPASSWORD", default="")
        database = env_value(env, "MYSQL_DATABASE", "MYSQLDATABASE", default="cute_food")

    missing = [
        name
        for name, value in (
            ("host", host),
            ("port", port),
            ("user", user),
            ("database", database),
        )
        if not value
    ]
    if missing:
        label = "ONLINE_MYSQL_*" if prefix else "MYSQL_*"
        raise ValueError(f"Missing {label} settings: {', '.join(missing)}")

    return {
        "host": host,
        "port": int(str(port)),
        "user": user,
        "password": password or "",
        "database": database,
        "charset": "utf8mb4",
    }


def connect(config: dict[str, object]):
    return mysql.connector.connect(**config)


def quote_identifier(name: str) -> str:
    return f"`{name.replace('`', '``')}`"


def table_columns(connection, table: str) -> set[str]:
    cursor = connection.cursor()
    try:
        cursor.execute(f"SHOW COLUMNS FROM {quote_identifier(table)}")
        return {row[0] for row in cursor.fetchall()}
    finally:
        cursor.close()


def ensure_mirror_schema(connection) -> None:
    cursor = connection.cursor()
    try:
        for statement in MIRROR_TABLE_SQL:
            cursor.execute(statement)
        connection.commit()
    finally:
        cursor.close()


def create_run(connection, online_config: dict[str, object]) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO online_user_sync_runs
              (started_at, source_host, source_database, status)
            VALUES (%s, %s, %s, %s)
            """,
            (datetime.now(), online_config["host"], online_config["database"], "running"),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        cursor.close()


def finish_run(connection, run_id: int, status: str, summary: dict, error_message: str | None = None) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            UPDATE online_user_sync_runs
            SET finished_at=%s, status=%s, summary=%s, error_message=%s
            WHERE id=%s
            """,
            (datetime.now(), status, json.dumps(summary, ensure_ascii=False), error_message, run_id),
        )
        connection.commit()
    finally:
        cursor.close()


def normalize_json_value(value):
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def fetch_source_rows(connection, spec: dict) -> list[dict]:
    available = table_columns(connection, spec["source"])
    missing_required = [source for source, _target, required in spec["columns"] if required and source not in available]
    if missing_required:
        raise RuntimeError(f"Remote table {spec['source']} is missing required columns: {', '.join(missing_required)}")

    selected_pairs = [(source, target) for source, target, _required in spec["columns"] if source in available]
    select_sql = ", ".join(f"{quote_identifier(source)} AS {quote_identifier(target)}" for source, target in selected_pairs)
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT {select_sql} FROM {quote_identifier(spec['source'])}")
        rows = cursor.fetchall()
    finally:
        cursor.close()

    target_columns = [target for _source, target, _required in spec["columns"]]
    normalized = []
    for row in rows:
        item = {target: row.get(target) for target in target_columns}
        for column in spec["json_columns"]:
            item[column] = normalize_json_value(item.get(column))
        normalized.append(item)
    return normalized


def upsert_rows(connection, spec: dict, rows: list[dict], run_id: int) -> int:
    if not rows:
        return 0

    now = datetime.now()
    target_columns = [target for _source, target, _required in spec["columns"]]
    write_columns = target_columns + ["last_synced_at", "last_seen_run_id"]
    placeholders = ", ".join(["%s"] * len(write_columns))
    columns_sql = ", ".join(quote_identifier(column) for column in write_columns)
    update_columns = [column for column in write_columns if column not in spec["primary_key"]]
    update_sql = ", ".join(f"{quote_identifier(column)}=VALUES({quote_identifier(column)})" for column in update_columns)
    sql = f"""
        INSERT INTO {quote_identifier(spec['target'])} ({columns_sql})
        VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE {update_sql}
    """
    values = [tuple(row.get(column) for column in target_columns) + (now, run_id) for row in rows]
    cursor = connection.cursor()
    try:
        cursor.executemany(sql, values)
        connection.commit()
        return len(values)
    finally:
        cursor.close()


def prune_missing_rows(connection, spec: dict, run_id: int) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"DELETE FROM {quote_identifier(spec['target'])} WHERE last_seen_run_id <> %s",
            (run_id,),
        )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        cursor.close()


def count_rows(connection, table: str) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}")
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


def sync_user_data(dry_run: bool = False, prune: bool = False) -> dict:
    env = read_env_file()
    local_config = database_config(env, "")
    online_config = database_config(env, "ONLINE_")
    summary: dict[str, dict[str, int]] = {}

    online = connect(online_config)
    local = None if dry_run else connect(local_config)
    run_id = 0

    try:
        if local:
            ensure_mirror_schema(local)
            run_id = create_run(local, online_config)

        for spec in TABLES:
            rows = fetch_source_rows(online, spec)
            table_summary = {"online_rows": len(rows), "synced_rows": 0, "pruned_rows": 0}
            if local:
                table_summary["synced_rows"] = upsert_rows(local, spec, rows, run_id)
                if prune:
                    table_summary["pruned_rows"] = prune_missing_rows(local, spec, run_id)
                table_summary["local_mirror_rows"] = count_rows(local, spec["target"])
            summary[spec["source"]] = table_summary

        if local:
            finish_run(local, run_id, "success", summary)
        return summary
    except Exception as error:
        if local and run_id:
            finish_run(local, run_id, "failed", summary, str(error))
        raise
    finally:
        online.close()
        if local:
            local.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync online user data into local online_* mirror tables.")
    parser.add_argument("--dry-run", action="store_true", help="Only count online rows; do not write local mirror tables.")
    parser.add_argument("--prune", action="store_true", help="Delete local mirror rows that no longer exist online.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        summary = sync_user_data(dry_run=args.dry_run, prune=args.prune)
    except Exception as error:
        print(f"Sync failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
