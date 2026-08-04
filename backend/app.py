import io
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from functools import wraps

import mysql.connector
from flask import Flask, has_request_context, jsonify, render_template, request, send_file, session
from mysql.connector import Error
from werkzeug.datastructures import FileStorage
from werkzeug.security import check_password_hash, generate_password_hash

from .catalogue import INGREDIENTS, RECIPES
from .excel_import import apply_import, read_upload, validate_payload
from .excel_export import build_catalogue_export


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "database" / "schema.sql"
ENV_PATH = ROOT / ".env"
BACKUPS_DIR = ROOT / "backups"
DEFAULT_USER_KEY = "browser-demo"
USER_ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9_-]{3,32}")
IMPORT_PREVIEW_TTL_SECONDS = 15 * 60
IMPORT_PREVIEWS = {}
DEFAULT_INGREDIENT_CATEGORIES = ("蔬菜", "肉类", "蛋奶", "豆制品", "菌菇", "水产", "主食", "调味料", "坚果", "其他")


def read_env_file():
    if not ENV_PATH.exists():
        return {}
    values = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def settings():
    env = read_env_file()
    get = lambda key, default: os.getenv(key, env.get(key, default))
    # Railway's MySQL template exports names such as MYSQLHOST; keep the
    # underscore names for local development while accepting those references.
    get_mysql = lambda standard, railway, default: get(standard, get(railway, default))
    return {
        "host": get_mysql("MYSQL_HOST", "MYSQLHOST", "127.0.0.1"),
        "port": int(get_mysql("MYSQL_PORT", "MYSQLPORT", "3306")),
        "user": get_mysql("MYSQL_USER", "MYSQLUSER", "food_app"),
        "password": get_mysql("MYSQL_PASSWORD", "MYSQLPASSWORD", ""),
        "database": get_mysql("MYSQL_DATABASE", "MYSQLDATABASE", "cute_food"),
        "flask_host": get("FLASK_HOST", "127.0.0.1"),
        "flask_port": int(os.getenv("PORT", get("FLASK_PORT", "3008"))),
        "frontend_origin": get("FRONTEND_ORIGIN", "http://localhost:5241").rstrip("/"),
        "cookie_secure": get("COOKIE_SECURE", "false").lower() == "true",
        "secret_key": get("FLASK_SECRET_KEY", "change-this-local-secret"),
        "admin_username": get("ADMIN_USERNAME", "admin"),
        "admin_password": get("ADMIN_PASSWORD", "Admin_2026!"),
    }


def database_name(config):
    name = config["database"]
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError("MYSQL_DATABASE 只能包含字母、数字和下划线")
    return name


def connect():
    config = settings()
    options = {key: config[key] for key in ("host", "port", "user", "password")}
    options["database"] = database_name(config)
    return mysql.connector.connect(**options)


def log_admin_action(connection, action, target_type, target_id=None, details=None):
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO admin_operation_logs (admin_id, action, target_type, target_id, details)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            session.get("admin_id"),
            action,
            target_type,
            target_id,
            json.dumps(details or {}, ensure_ascii=False),
        ),
    )
    cursor.close()


def create_catalogue_backup(connection, backup_type, label, admin_id=None):
    """Persist a downloadable catalogue snapshot before a risky admin action."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    file_name = f"catalogue-{backup_type}-{timestamp}.xlsx"
    output_path = BACKUPS_DIR / file_name
    workbook = build_catalogue_export(connection)
    output_path.write_bytes(workbook.getvalue())

    cursor = connection.cursor(dictionary=True)
    summary = {}
    for key, table in (("recipes", "recipes"), ("ingredients", "ingredients"), ("categories", "ingredient_categories")):
        cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
        summary[key] = cursor.fetchone()["count"]
    cursor.close()

    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO catalogue_backups (backup_type, label, file_name, file_path, summary, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            backup_type,
            label,
            file_name,
            str(Path("backups") / file_name),
            json.dumps(summary, ensure_ascii=False),
            admin_id if admin_id is not None else (session.get("admin_id") if has_request_context() else None),
        ),
    )
    backup_id = cursor.lastrowid
    cursor.close()
    return {"id": backup_id, "file_name": file_name, "summary": summary}


def backup_file_path(record):
    candidate = (ROOT / record["file_path"]).resolve()
    root = BACKUPS_DIR.resolve()
    if root not in (candidate, *candidate.parents):
        return None
    return candidate


def restore_catalogue_backup(connection, path):
    file_storage = FileStorage(stream=io.BytesIO(path.read_bytes()), filename=path.name)
    data, upload_errors = read_upload(file_storage)
    if data is None:
        return upload_errors, {}
    categories = {item["分类*"] for item in data["ingredients"]}
    cursor = connection.cursor()
    cursor.executemany("INSERT IGNORE INTO ingredient_categories (name) VALUES (%s)", [(name,) for name in categories])
    connection.commit()
    cursor.close()
    errors, summary = validate_payload(data, connection, upload_errors, ingredient_category_names(connection))
    if errors:
        return errors, summary
    apply_import(data, connection)
    return [], summary


def maybe_create_daily_catalogue_backup(connection):
    cursor = connection.cursor()
    cursor.execute(
        "SELECT id FROM catalogue_backups WHERE backup_type='scheduled' AND DATE(created_at)=CURDATE() LIMIT 1"
    )
    already_created = cursor.fetchone()
    cursor.close()
    if already_created:
        return None
    backup = create_catalogue_backup(connection, "scheduled", "每日自动菜谱数据快照")
    log_admin_action(connection, "backup.scheduled", "catalogue_backup", str(backup["id"]), backup["summary"])
    connection.commit()
    return backup


def run_schema_and_seed():
    connection = connect()
    cursor = connection.cursor()
    for statement in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            cursor.execute(statement)
    connection.commit()
    ensure_user_auth_columns(connection)
    cursor.close()
    connection.close()
    seed_catalogue()
    connection = connect()
    ensure_ingredient_categories(connection)
    connection.close()
    ensure_initial_admin()


def ensure_user_auth_columns(connection):
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME='users'
        """,
        (settings()["database"],),
    )
    existing = {row[0] for row in cursor.fetchall()}
    additions = {
        "password_hash": "ADD COLUMN password_hash VARCHAR(255) NULL AFTER display_name",
        "role": "ADD COLUMN role ENUM('user', 'admin') NOT NULL DEFAULT 'user' AFTER password_hash",
        "is_active": "ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE AFTER role",
    }
    for name, statement in additions.items():
        if name not in existing:
            cursor.execute(f"ALTER TABLE users {statement}")
    connection.commit()
    cursor.close()


def ensure_initial_admin():
    config = settings()
    connection = connect()
    cursor = connection.cursor(dictionary=True)
    cursor.execute("SELECT id FROM users WHERE external_key=%s", (config["admin_username"],))
    admin = cursor.fetchone()
    if admin:
        cursor.execute(
            "UPDATE users SET role='admin', is_active=TRUE WHERE id=%s",
            (admin["id"],),
        )
    else:
        cursor.execute(
            """
            INSERT INTO users (external_key, display_name, password_hash, role, is_active)
            VALUES (%s, %s, %s, 'admin', TRUE)
            """,
            (config["admin_username"], "小锅管理员", generate_password_hash(config["admin_password"])),
        )
    connection.commit()
    cursor.close()
    connection.close()


def seed_catalogue():
    connection = connect()
    cursor = connection.cursor()
    for ingredient in INGREDIENTS:
        cursor.execute(
            """
            INSERT INTO ingredients (id, name, category, icon) VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE name=VALUES(name), category=VALUES(category), icon=VALUES(icon)
            """,
            (ingredient["id"], ingredient["name"], ingredient["category"], ingredient["icon"]),
        )
    for recipe in RECIPES:
        cursor.execute(
            """
            INSERT INTO recipes (id, name, art, description, minutes, difficulty, calories, tags)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              name=VALUES(name),
              art=VALUES(art),
              description=VALUES(description),
              minutes=VALUES(minutes),
              difficulty=VALUES(difficulty),
              calories=VALUES(calories),
              tags=VALUES(tags)
            """,
            (recipe["id"], recipe["name"], recipe["art"], recipe["description"], recipe["minutes"], recipe["difficulty"], recipe["calories"], json.dumps(recipe["tags"], ensure_ascii=False)),
        )
        cursor.execute("DELETE FROM recipe_ingredients WHERE recipe_id=%s", (recipe["id"],))
        cursor.execute("DELETE FROM recipe_steps WHERE recipe_id=%s", (recipe["id"],))
        for position, ingredient_id in enumerate(recipe["ingredients"], start=1):
            cursor.execute(
                "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, position) VALUES (%s, %s, %s)",
                (recipe["id"], ingredient_id, position),
            )
        for step_number, instruction in enumerate(recipe["steps"], start=1):
            cursor.execute(
                "INSERT INTO recipe_steps (recipe_id, step_number, instruction) VALUES (%s, %s, %s)",
                (recipe["id"], step_number, instruction),
            )
    connection.commit()
    cursor.close()
    connection.close()


def user_id(connection, external_key=None):
    if external_key is None and session.get("user_id"):
        return int(session["user_id"])
    key = external_key or request.headers.get("X-User-Key", DEFAULT_USER_KEY)
    cursor = connection.cursor(dictionary=True)
    cursor.execute("INSERT IGNORE INTO users (external_key) VALUES (%s)", (key,))
    cursor.execute("SELECT id FROM users WHERE external_key=%s", (key,))
    row = cursor.fetchone()
    cursor.close()
    return row["id"]


def current_user_id():
    value = session.get("user_id")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def user_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method == "OPTIONS":
            return view(*args, **kwargs)
        account_id = current_user_id()
        if not account_id:
            return json_error("请先登录后再收藏菜谱", 401)
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT external_key, role, is_active FROM users WHERE id=%s", (account_id,))
        account = cursor.fetchone()
        cursor.close()
        connection.close()
        if not account or not account["is_active"] or account["role"] != "user":
            session.pop("user_id", None)
            session.pop("user_username", None)
            return json_error("当前账号不可用，请重新登录", 401)
        return view(*args, **kwargs)
    return wrapped


def record_user_auth_event(connection, account_id, event_type):
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO user_auth_events (user_id, event_type) VALUES (%s, %s)",
        (account_id, event_type),
    )
    cursor.close()


def valid_user_credentials(username, password):
    if not USER_ACCOUNT_PATTERN.fullmatch(username):
        return "账号请使用 3-32 位字母、数字、下划线或短横线"
    if not isinstance(password, str) or len(password) < 6 or len(password) > 72:
        return "密码请使用 6-72 位字符"
    return None


def decode_json(value, fallback):
    if isinstance(value, list):
        return value
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def fetch_recipes(connection):
    cursor = connection.cursor(dictionary=True)
    cursor.execute("SELECT * FROM recipes ORDER BY minutes, name")
    rows = cursor.fetchall()
    recipes = []
    for row in rows:
        cursor.execute(
            """
            SELECT i.id, i.name, i.category, i.icon
            FROM recipe_ingredients ri JOIN ingredients i ON i.id=ri.ingredient_id
            WHERE ri.recipe_id=%s ORDER BY ri.position
            """,
            (row["id"],),
        )
        ingredients = cursor.fetchall()
        cursor.execute("SELECT instruction FROM recipe_steps WHERE recipe_id=%s ORDER BY step_number", (row["id"],))
        steps = [item["instruction"] for item in cursor.fetchall()]
        recipes.append({
            "id": row["id"], "name": row["name"], "art": row["art"], "desc": row["description"],
            "minutes": row["minutes"], "time": f"{row['minutes']} 分钟", "diff": row["difficulty"],
            "cal": f"{row['calories']} kcal", "calories": row["calories"], "tags": decode_json(row["tags"], []),
            "ingredients": ingredients, "ingredient_ids": [item["id"] for item in ingredients],
            "ingredient_names": [item["name"] for item in ingredients], "steps": steps,
        })
    cursor.close()
    return recipes


def json_error(message, status=400):
    return jsonify({"error": message}), status


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            return json_error("请先以管理员身份登录", 401)
        return view(*args, **kwargs)
    return wrapped


def valid_catalogue_id(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9-]{2,60}", value)


def cleanup_import_previews():
    now = time.time()
    for token, preview in list(IMPORT_PREVIEWS.items()):
        if preview["expires_at"] <= now:
            del IMPORT_PREVIEWS[token]


def ensure_ingredient_categories(connection):
    cursor = connection.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ingredient_categories (
          name VARCHAR(40) PRIMARY KEY,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.executemany(
        "INSERT IGNORE INTO ingredient_categories (name) VALUES (%s)",
        [(name,) for name in DEFAULT_INGREDIENT_CATEGORIES],
    )
    cursor.execute(
        """
        INSERT IGNORE INTO ingredient_categories (name)
        SELECT DISTINCT category FROM ingredients WHERE category <> ''
        """
    )
    connection.commit()
    cursor.close()


def ingredient_category_names(connection):
    ensure_ingredient_categories(connection)
    cursor = connection.cursor()
    cursor.execute("SELECT name FROM ingredient_categories ORDER BY name")
    names = [row[0] for row in cursor.fetchall()]
    cursor.close()
    return names


def list_admin_ingredients(connection):
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT i.id, i.name, i.category, i.icon, COUNT(ri.recipe_id) AS recipe_count
        FROM ingredients i LEFT JOIN recipe_ingredients ri ON ri.ingredient_id=i.id
        GROUP BY i.id ORDER BY i.category, i.name
        """
    )
    rows = cursor.fetchall()
    cursor.close()
    return rows


def save_recipe(connection, payload, is_new):
    recipe_id = payload.get("id", "")
    ingredient_ids = payload.get("ingredient_ids", [])
    steps = payload.get("steps", [])
    tags = payload.get("tags", [])
    required = ("name", "art", "desc", "minutes", "diff", "calories")
    if not valid_catalogue_id(recipe_id) or any(not payload.get(key) for key in required):
        return "请填写菜谱编号、名称、插画、简介、时长、难度和热量"
    if not isinstance(ingredient_ids, list) or not ingredient_ids or len(ingredient_ids) != len(set(ingredient_ids)):
        return "至少选择一种不重复的食材"
    if not isinstance(steps, list) or not [step for step in steps if str(step).strip()]:
        return "至少填写一个烹饪步骤"
    if not isinstance(tags, list):
        return "标签格式不正确"
    try:
        minutes = int(payload["minutes"])
        calories = int(payload["calories"])
    except (TypeError, ValueError):
        return "时长和热量必须是数字"
    if minutes < 1 or calories < 0:
        return "时长和热量不能小于 0"

    cursor = connection.cursor(dictionary=True)
    cursor.execute("SELECT id FROM ingredients WHERE id IN ({})".format(",".join(["%s"] * len(ingredient_ids))), ingredient_ids)
    if {row["id"] for row in cursor.fetchall()} != set(ingredient_ids):
        cursor.close()
        return "包含不存在的食材"
    if is_new:
        cursor.execute("SELECT id FROM recipes WHERE id=%s", (recipe_id,))
        if cursor.fetchone():
            cursor.close()
            return "这个菜谱编号已经存在"
        cursor.execute(
            "INSERT INTO recipes (id, name, art, description, minutes, difficulty, calories, tags) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (recipe_id, payload["name"], payload["art"], payload["desc"], minutes, payload["diff"], calories, json.dumps(tags, ensure_ascii=False)),
        )
    else:
        cursor.execute("SELECT id FROM recipes WHERE id=%s", (recipe_id,))
        if not cursor.fetchone():
            cursor.close()
            return "要编辑的菜谱不存在"
        cursor.execute(
            "UPDATE recipes SET name=%s, art=%s, description=%s, minutes=%s, difficulty=%s, calories=%s, tags=%s WHERE id=%s",
            (payload["name"], payload["art"], payload["desc"], minutes, payload["diff"], calories, json.dumps(tags, ensure_ascii=False), recipe_id),
        )
        cursor.execute("DELETE FROM recipe_ingredients WHERE recipe_id=%s", (recipe_id,))
        cursor.execute("DELETE FROM recipe_steps WHERE recipe_id=%s", (recipe_id,))
    cursor.executemany(
        "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, position) VALUES (%s, %s, %s)",
        [(recipe_id, ingredient_id, index) for index, ingredient_id in enumerate(ingredient_ids, start=1)],
    )
    cursor.executemany(
        "INSERT INTO recipe_steps (recipe_id, step_number, instruction) VALUES (%s, %s, %s)",
        [(recipe_id, index, str(step).strip()) for index, step in enumerate(steps, start=1) if str(step).strip()],
    )
    log_admin_action(
        connection,
        "recipe.create" if is_new else "recipe.update",
        "recipe",
        recipe_id,
        {"name": payload["name"], "ingredient_count": len(ingredient_ids), "step_count": len(steps)},
    )
    connection.commit()
    cursor.close()
    return None


def create_app():
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    config = settings()
    app.secret_key = config["secret_key"]
    app.config["SESSION_COOKIE_SAMESITE"] = "None" if config["cookie_secure"] else "Lax"
    app.config["SESSION_COOKIE_SECURE"] = config["cookie_secure"]

    @app.after_request
    def allow_frontend(response):
        origin = request.headers.get("Origin", "").rstrip("/")
        local_frontend_origins = {"http://localhost:5241", "http://127.0.0.1:5241"}
        allowed_origins = {*local_frontend_origins, config["frontend_origin"]}
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
        elif (
            origin == "null"
            and config["frontend_origin"] in local_frontend_origins
            and config["flask_host"] in {"127.0.0.1", "localhost"}
        ):
            response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-User-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    @app.errorhandler(Error)
    def mysql_error(error):
        return json_error(f"数据库连接失败：{error.msg}", 503)

    @app.route("/api/health")
    def health():
        connection = connect()
        connection.close()
        return jsonify({"status": "ok", "database": settings()["database"]})

    @app.route("/api/ping")
    def ping():
        """Lightweight platform liveness check that does not require MySQL to be ready yet."""
        return jsonify({"status": "ok"})

    @app.route("/")
    def frontend_page():
        return send_file(ROOT / "index.html")

    @app.route("/config.js")
    def frontend_config():
        return app.response_class(
            "window.CUTE_FOOD_CONFIG = { API_ORIGIN: window.location.origin };\n",
            mimetype="application/javascript",
        )

    @app.route("/admin")
    def admin_page():
        return render_template("admin.html")

    @app.route("/api/auth/me")
    def auth_me():
        if not session.get("admin_id"):
            return jsonify({"authenticated": False})
        return jsonify({"authenticated": True, "username": session.get("admin_username")})

    @app.route("/api/auth/login", methods=["POST"])
    def auth_login():
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", "")).strip()
        password = payload.get("password", "")
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, external_key, password_hash, role, is_active FROM users WHERE external_key=%s",
            (username,),
        )
        account = cursor.fetchone()
        cursor.close(); connection.close()
        if not account or not account["is_active"] or account["role"] != "admin" or not account["password_hash"]:
            return json_error("账号或密码不正确", 401)
        if not check_password_hash(account["password_hash"], password):
            return json_error("账号或密码不正确", 401)
        session.clear()
        session["admin_id"] = account["id"]
        session["admin_username"] = account["external_key"]
        return jsonify({"authenticated": True, "username": account["external_key"]})

    @app.route("/api/auth/logout", methods=["POST"])
    def auth_logout():
        session.clear()
        return "", 204

    @app.route("/api/user-auth/me")
    def user_auth_me():
        account_id = current_user_id()
        if not account_id:
            return jsonify({"authenticated": False})
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, external_key, is_active, role FROM users WHERE id=%s",
            (account_id,),
        )
        account = cursor.fetchone()
        cursor.close()
        connection.close()
        if not account or not account["is_active"] or account["role"] != "user":
            session.pop("user_id", None)
            session.pop("user_username", None)
            return jsonify({"authenticated": False})
        return jsonify({"authenticated": True, "username": account["external_key"]})

    @app.route("/api/user-auth/register", methods=["POST"])
    def user_auth_register():
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", "")).strip()
        password = payload.get("password", "")
        validation_error = valid_user_credentials(username, password)
        if validation_error:
            return json_error(validation_error)
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id FROM users WHERE external_key=%s", (username,))
        if cursor.fetchone():
            cursor.close()
            connection.close()
            return json_error("这个账号已经被使用了，换一个试试吧", 409)
        cursor.close()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO users (external_key, display_name, password_hash, role, is_active)
                VALUES (%s, %s, %s, 'user', TRUE)
                """,
                (username, username, generate_password_hash(password)),
            )
            account_id = cursor.lastrowid
            record_user_auth_event(connection, account_id, "register")
            record_user_auth_event(connection, account_id, "login")
            connection.commit()
        except Error:
            connection.rollback()
            cursor.close()
            connection.close()
            return json_error("注册没有完成，请换一个账号后再试", 409)
        cursor.close()
        connection.close()
        session["user_id"] = account_id
        session["user_username"] = username
        return jsonify({"authenticated": True, "username": username}), 201

    @app.route("/api/user-auth/login", methods=["POST"])
    def user_auth_login():
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", "")).strip()
        password = payload.get("password", "")
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, external_key, password_hash, role, is_active FROM users WHERE external_key=%s",
            (username,),
        )
        account = cursor.fetchone()
        cursor.close()
        if (
            not account
            or not account["is_active"]
            or account["role"] != "user"
            or not account["password_hash"]
            or not check_password_hash(account["password_hash"], password)
        ):
            connection.close()
            return json_error("账号或密码不正确", 401)
        record_user_auth_event(connection, account["id"], "login")
        connection.commit()
        connection.close()
        session["user_id"] = account["id"]
        session["user_username"] = account["external_key"]
        return jsonify({"authenticated": True, "username": account["external_key"]})

    @app.route("/api/user-auth/logout", methods=["POST"])
    def user_auth_logout():
        account_id = current_user_id()
        if account_id:
            connection = connect()
            record_user_auth_event(connection, account_id, "logout")
            connection.commit()
            connection.close()
        session.pop("user_id", None)
        session.pop("user_username", None)
        return "", 204

    @app.route("/api/auth/password", methods=["POST"])
    @admin_required
    def auth_password():
        payload = request.get_json(silent=True) or {}
        current_password = payload.get("current_password", "")
        new_password = payload.get("new_password", "")
        if not isinstance(new_password, str) or len(new_password) < 10:
            return json_error("新密码至少需要 10 位")
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT password_hash FROM users WHERE id=%s", (session["admin_id"],))
        account = cursor.fetchone()
        if not account or not check_password_hash(account["password_hash"], current_password):
            cursor.close(); connection.close()
            return json_error("当前密码不正确", 401)
        cursor.execute("UPDATE users SET password_hash=%s WHERE id=%s", (generate_password_hash(new_password), session["admin_id"]))
        log_admin_action(connection, "admin.change_password", "admin", str(session["admin_id"]))
        connection.commit(); cursor.close(); connection.close()
        return jsonify({"updated": True})

    @app.route("/api/admin/summary")
    @admin_required
    def admin_summary():
        connection = connect()
        maybe_create_daily_catalogue_backup(connection)
        cursor = connection.cursor(dictionary=True)
        counts = {}
        for key, table in (("recipes", "recipes"), ("ingredients", "ingredients"), ("users", "users"), ("records", "cooking_records")):
            cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
            counts[key] = cursor.fetchone()["count"]
        cursor.close(); connection.close()
        return jsonify(counts)

    @app.route("/api/admin/ingredients", methods=["GET", "POST"])
    @admin_required
    def admin_ingredients():
        connection = connect()
        categories = ingredient_category_names(connection)
        if request.method == "GET":
            rows = list_admin_ingredients(connection)
            connection.close()
            return jsonify(rows)
        payload = request.get_json(silent=True) or {}
        if not valid_catalogue_id(payload.get("id")) or not all(str(payload.get(key, "")).strip() for key in ("name", "category", "icon")):
            connection.close()
            return json_error("请填写英文编号、名称、分类和图标")
        if payload["category"].strip() not in categories:
            connection.close()
            return json_error("该分类尚未在分类管理中创建")
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO ingredients (id, name, category, icon) VALUES (%s, %s, %s, %s)",
                (payload["id"], payload["name"].strip(), payload["category"].strip(), payload["icon"].strip()),
            )
            log_admin_action(connection, "ingredient.create", "ingredient", payload["id"], {"name": payload["name"].strip()})
            connection.commit()
        except Error as error:
            connection.rollback(); cursor.close(); connection.close()
            return json_error(f"保存食材失败：{error.msg}")
        cursor.close(); connection.close()
        return jsonify({"id": payload["id"]}), 201

    @app.route("/api/admin/ingredients/<ingredient_id>", methods=["PUT", "DELETE"])
    @admin_required
    def admin_ingredient(ingredient_id):
        connection = connect()
        if request.method == "DELETE":
            cursor = connection.cursor()
            try:
                cursor.execute("DELETE FROM ingredients WHERE id=%s", (ingredient_id,))
                log_admin_action(connection, "ingredient.delete", "ingredient", ingredient_id)
                connection.commit()
            except Error as error:
                connection.rollback(); cursor.close(); connection.close()
                return json_error("该食材正被菜谱使用，不能删除", 409)
            cursor.close(); connection.close()
            return "", 204
        payload = request.get_json(silent=True) or {}
        if not all(str(payload.get(key, "")).strip() for key in ("name", "category", "icon")):
            connection.close()
            return json_error("请填写名称、分类和图标")
        if payload["category"].strip() not in ingredient_category_names(connection):
            connection.close()
            return json_error("该分类尚未在分类管理中创建")
        cursor = connection.cursor()
        cursor.execute("UPDATE ingredients SET name=%s, category=%s, icon=%s WHERE id=%s", (payload["name"].strip(), payload["category"].strip(), payload["icon"].strip(), ingredient_id))
        log_admin_action(connection, "ingredient.update", "ingredient", ingredient_id, {"name": payload["name"].strip()})
        connection.commit(); cursor.close(); connection.close()
        return jsonify({"id": ingredient_id})

    @app.route("/api/admin/ingredient-categories", methods=["GET", "POST"])
    @admin_required
    def admin_ingredient_categories():
        connection = connect()
        categories = ingredient_category_names(connection)
        if request.method == "GET":
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT c.name, COUNT(i.id) AS ingredient_count
                FROM ingredient_categories c LEFT JOIN ingredients i ON i.category=c.name
                GROUP BY c.name ORDER BY c.name
                """
            )
            rows = cursor.fetchall()
            cursor.close(); connection.close()
            return jsonify(rows)
        name = str((request.get_json(silent=True) or {}).get("name", "")).strip()
        if not name or len(name) > 40:
            connection.close()
            return json_error("分类名称不能为空且不能超过 40 个字符")
        if name in categories:
            connection.close()
            return json_error("该分类已存在")
        cursor = connection.cursor()
        cursor.execute("INSERT INTO ingredient_categories (name) VALUES (%s)", (name,))
        log_admin_action(connection, "category.create", "ingredient_category", name)
        connection.commit(); cursor.close(); connection.close()
        return jsonify({"name": name}), 201

    @app.route("/api/admin/ingredient-categories/<path:category_name>", methods=["DELETE"])
    @admin_required
    def admin_ingredient_category(category_name):
        connection = connect()
        ingredient_category_names(connection)
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM ingredients WHERE category=%s", (category_name,))
        if cursor.fetchone()[0]:
            cursor.close(); connection.close()
            return json_error("该分类仍有食材，不能删除", 409)
        cursor.execute("DELETE FROM ingredient_categories WHERE name=%s", (category_name,))
        log_admin_action(connection, "category.delete", "ingredient_category", category_name)
        connection.commit(); cursor.close(); connection.close()
        return "", 204

    @app.route("/api/admin/recipes", methods=["GET", "POST"])
    @admin_required
    def admin_recipes():
        connection = connect()
        if request.method == "GET":
            rows = fetch_recipes(connection)
            connection.close()
            return jsonify(rows)
        error = save_recipe(connection, request.get_json(silent=True) or {}, True)
        connection.close()
        return json_error(error) if error else jsonify({"saved": True}), 201

    @app.route("/api/admin/recipes/<recipe_id>", methods=["PUT", "DELETE"])
    @admin_required
    def admin_recipe(recipe_id):
        connection = connect()
        if request.method == "DELETE":
            cursor = connection.cursor()
            cursor.execute("DELETE FROM recipes WHERE id=%s", (recipe_id,))
            log_admin_action(connection, "recipe.delete", "recipe", recipe_id)
            connection.commit(); cursor.close(); connection.close()
            return "", 204
        payload = request.get_json(silent=True) or {}
        payload["id"] = recipe_id
        error = save_recipe(connection, payload, False)
        connection.close()
        return json_error(error) if error else jsonify({"saved": True})

    @app.route("/api/admin/export.xlsx")
    @admin_required
    def admin_export_catalogue():
        connection = connect()
        try:
            workbook = build_catalogue_export(connection)
        finally:
            connection.close()
        return send_file(
            workbook,
            as_attachment=True,
            download_name="今天吃什么呀-数据库完整导出.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            max_age=0,
        )

    @app.route("/api/admin/import/preview", methods=["POST"])
    @admin_required
    def admin_import_preview():
        cleanup_import_previews()
        data, upload_errors = read_upload(request.files.get("file"))
        if data is None:
            return jsonify({"valid": False, "summary": {}, "errors": upload_errors})
        connection = connect()
        errors, summary = validate_payload(data, connection, upload_errors, ingredient_category_names(connection))
        connection.close()
        if errors:
            return jsonify({"valid": False, "summary": summary, "errors": errors})

        token = secrets.token_urlsafe(24)
        IMPORT_PREVIEWS[token] = {
            "admin_id": session["admin_id"],
            "data": data,
            "summary": summary,
            "expires_at": time.time() + IMPORT_PREVIEW_TTL_SECONDS,
        }
        return jsonify({"valid": True, "token": token, "summary": summary, "errors": []})

    @app.route("/api/admin/import/commit", methods=["POST"])
    @admin_required
    def admin_import_commit():
        cleanup_import_previews()
        token = (request.get_json(silent=True) or {}).get("token")
        preview = IMPORT_PREVIEWS.get(token)
        if not preview or preview["admin_id"] != session["admin_id"]:
            return json_error("导入预览已失效，请重新上传 Excel")

        connection = connect()
        errors, summary = validate_payload(preview["data"], connection, categories=ingredient_category_names(connection))
        if errors:
            connection.close()
            del IMPORT_PREVIEWS[token]
            return json_error("数据库数据已变化，请重新上传并预览 Excel")
        try:
            backup = create_catalogue_backup(connection, "import", "Excel 导入前自动快照")
            connection.commit()
            apply_import(preview["data"], connection)
            log_admin_action(
                connection,
                "catalogue.import",
                "catalogue_backup",
                str(backup["id"]),
                {"backup_id": backup["id"], "summary": summary},
            )
            connection.commit()
        except Error as error:
            connection.close()
            return json_error(f"导入失败，所有数据已回滚：{error.msg}", 500)
        except Exception:
            connection.close()
            return json_error("导入失败，所有数据已回滚", 500)
        connection.close()
        del IMPORT_PREVIEWS[token]
        return jsonify({"imported": True, "summary": summary, "backup_id": backup["id"]})

    @app.route("/api/admin/backups", methods=["GET", "POST"])
    @admin_required
    def admin_backups():
        connection = connect()
        if request.method == "POST":
            backup = create_catalogue_backup(connection, "manual", "管理员手动菜谱数据快照")
            log_admin_action(connection, "backup.manual", "catalogue_backup", str(backup["id"]), backup["summary"])
            connection.commit()
            connection.close()
            return jsonify({"created": True, "backup": backup}), 201
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT b.id, b.backup_type, b.label, b.file_name, b.summary, b.created_at,
                   u.external_key AS created_by_name
            FROM catalogue_backups b LEFT JOIN users u ON u.id=b.created_by
            ORDER BY b.created_at DESC LIMIT 100
            """
        )
        rows = cursor.fetchall()
        cursor.close(); connection.close()
        for row in rows:
            row["summary"] = decode_json(row["summary"], {})
        return jsonify(rows)

    @app.route("/api/admin/backups/<int:backup_id>/download")
    @admin_required
    def admin_backup_download(backup_id):
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT file_name, file_path FROM catalogue_backups WHERE id=%s", (backup_id,))
        backup = cursor.fetchone()
        cursor.close()
        if not backup:
            connection.close()
            return json_error("备份不存在", 404)
        path = backup_file_path(backup)
        if not path or not path.is_file():
            connection.close()
            return json_error("备份文件不存在，请重新创建备份", 404)
        log_admin_action(connection, "backup.download", "catalogue_backup", str(backup_id))
        connection.commit(); connection.close()
        return send_file(path, as_attachment=True, download_name=backup["file_name"], max_age=0)

    @app.route("/api/admin/backups/<int:backup_id>/restore", methods=["POST"])
    @admin_required
    def admin_backup_restore(backup_id):
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, file_name, file_path FROM catalogue_backups WHERE id=%s", (backup_id,))
        backup = cursor.fetchone()
        cursor.close()
        if not backup:
            connection.close()
            return json_error("备份不存在", 404)
        path = backup_file_path(backup)
        if not path or not path.is_file():
            connection.close()
            return json_error("备份文件不存在，请重新创建备份", 404)
        try:
            before_restore = create_catalogue_backup(connection, "restore", "恢复前自动快照")
            connection.commit()
            errors, summary = restore_catalogue_backup(connection, path)
            if errors:
                connection.close()
                return jsonify({"restored": False, "errors": errors, "summary": summary}), 422
            log_admin_action(
                connection,
                "backup.restore",
                "catalogue_backup",
                str(backup_id),
                {"restore_before_backup_id": before_restore["id"], "summary": summary},
            )
            connection.commit()
        except Error as error:
            connection.close()
            return json_error(f"恢复失败，当前数据未被覆盖：{error.msg}", 500)
        except Exception:
            connection.close()
            return json_error("恢复失败，当前数据未被覆盖", 500)
        connection.close()
        return jsonify({"restored": True, "summary": summary, "before_restore_backup_id": before_restore["id"]})

    @app.route("/api/admin/audit-logs")
    @admin_required
    def admin_audit_logs():
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT l.id, l.action, l.target_type, l.target_id, l.details, l.created_at,
                   u.external_key AS admin_name
            FROM admin_operation_logs l LEFT JOIN users u ON u.id=l.admin_id
            ORDER BY l.created_at DESC LIMIT 200
            """
        )
        rows = cursor.fetchall()
        cursor.close(); connection.close()
        for row in rows:
            row["details"] = decode_json(row["details"], {})
        return jsonify(rows)

    @app.route("/api/admin/users")
    @admin_required
    def admin_users():
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, external_key, display_name, role, is_active, created_at FROM users ORDER BY created_at DESC")
        rows = cursor.fetchall()
        cursor.close(); connection.close()
        return jsonify(rows)

    @app.route("/api/admin/users/<int:account_id>", methods=["PATCH"])
    @admin_required
    def admin_user(account_id):
        payload = request.get_json(silent=True) or {}
        role = payload.get("role")
        is_active = payload.get("is_active")
        if role not in ("user", "admin") or not isinstance(is_active, bool):
            return json_error("角色或账号状态不正确")
        if account_id == session["admin_id"] and (role != "admin" or not is_active):
            return json_error("不能撤销当前登录管理员的权限")
        connection = connect()
        cursor = connection.cursor()
        cursor.execute("UPDATE users SET role=%s, is_active=%s WHERE id=%s", (role, is_active, account_id))
        log_admin_action(connection, "user.update_access", "user", str(account_id), {"role": role, "is_active": is_active})
        connection.commit(); cursor.close(); connection.close()
        return jsonify({"saved": True})

    @app.route("/api/admin/cooking-records")
    @admin_required
    def admin_cooking_records():
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT c.id, u.external_key AS user_key, r.name AS recipe_name, c.rating, c.notes, c.finished_at
            FROM cooking_records c JOIN users u ON u.id=c.user_id JOIN recipes r ON r.id=c.recipe_id
            ORDER BY c.finished_at DESC LIMIT 100
            """
        )
        rows = cursor.fetchall()
        cursor.close(); connection.close()
        return jsonify(rows)

    @app.route("/api/ingredients")
    def ingredients():
        connection = connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, name, category, icon FROM ingredients ORDER BY category, id")
        rows = cursor.fetchall()
        cursor.close()
        connection.close()
        return jsonify(rows)

    @app.route("/api/recipes")
    def recipes():
        requested = [item for item in request.args.get("ingredient_ids", "").split(",") if item]
        connection = connect()
        all_recipes = fetch_recipes(connection)
        connection.close()
        requested_set = set(requested)
        matching = [recipe for recipe in all_recipes if requested_set.issubset(recipe["ingredient_ids"])]
        for recipe in matching:
            recipe["existing_ingredient_ids"] = [item for item in recipe["ingredient_ids"] if item in requested_set]
            recipe["missing_ingredient_ids"] = [item for item in recipe["ingredient_ids"] if item not in requested_set]
        return jsonify(matching)

    @app.route("/api/recipes/<recipe_id>")
    def recipe_detail(recipe_id):
        connection = connect()
        recipe = next((item for item in fetch_recipes(connection) if item["id"] == recipe_id), None)
        connection.close()
        return jsonify(recipe) if recipe else json_error("菜谱不存在", 404)

    @app.route("/api/selection", methods=["GET", "POST", "OPTIONS"])
    def selection():
        if request.method == "OPTIONS":
            return "", 204
        connection = connect()
        account_id = user_id(connection)
        cursor = connection.cursor(dictionary=True)
        if request.method == "GET":
            cursor.execute("SELECT ingredient_id FROM user_selected_ingredients WHERE user_id=%s ORDER BY selected_at", (account_id,))
            selected = [row["ingredient_id"] for row in cursor.fetchall()]
            cursor.close(); connection.close()
            return jsonify({"ingredient_ids": selected})
        payload = request.get_json(silent=True) or {}
        selected = payload.get("ingredient_ids", [])
        if not isinstance(selected, list) or len(selected) > 5 or len(selected) != len(set(selected)):
            cursor.close(); connection.close()
            return json_error("请传入不重复的 1 至 5 种食材")
        cursor.execute("SELECT id FROM ingredients")
        valid_ids = {row["id"] for row in cursor.fetchall()}
        if not set(selected).issubset(valid_ids):
            cursor.close(); connection.close()
            return json_error("选到了不存在的食材")
        if selected:
            compatible = any(set(selected).issubset(recipe["ingredient_ids"]) for recipe in fetch_recipes(connection))
            if not compatible:
                cursor.close(); connection.close()
                return json_error("这组食材暂时凑不成一道菜，请换一位食材朋友吧～", 422)
        cursor.execute("DELETE FROM user_selected_ingredients WHERE user_id=%s", (account_id,))
        cursor.executemany(
            "INSERT INTO user_selected_ingredients (user_id, ingredient_id) VALUES (%s, %s)",
            [(account_id, ingredient_id) for ingredient_id in selected],
        )
        connection.commit()
        cursor.close(); connection.close()
        return jsonify({"ingredient_ids": selected})

    @app.route("/api/favorites", methods=["GET", "OPTIONS"])
    @user_required
    def favorites():
        if request.method == "OPTIONS":
            return "", 204
        connection = connect()
        account_id = current_user_id()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT recipe_id FROM user_favorites WHERE user_id=%s ORDER BY created_at DESC", (account_id,))
        recipe_ids = [row["recipe_id"] for row in cursor.fetchall()]
        cursor.close(); connection.close()
        return jsonify({"recipe_ids": recipe_ids})

    @app.route("/api/favorites/<recipe_id>", methods=["POST", "DELETE", "OPTIONS"])
    @user_required
    def favorite(recipe_id):
        if request.method == "OPTIONS":
            return "", 204
        connection = connect()
        account_id = current_user_id()
        cursor = connection.cursor()
        if request.method == "POST":
            cursor.execute("INSERT IGNORE INTO user_favorites (user_id, recipe_id) VALUES (%s, %s)", (account_id, recipe_id))
        else:
            cursor.execute("DELETE FROM user_favorites WHERE user_id=%s AND recipe_id=%s", (account_id, recipe_id))
        connection.commit()
        cursor.close(); connection.close()
        return jsonify({"recipe_id": recipe_id, "favorited": request.method == "POST"})

    @app.route("/api/cooking-records", methods=["GET", "POST", "OPTIONS"])
    @user_required
    def cooking_records():
        if request.method == "OPTIONS":
            return "", 204
        connection = connect()
        account_id = current_user_id()
        cursor = connection.cursor(dictionary=True)
        if request.method == "GET":
            cursor.execute(
                """
                SELECT c.id, c.recipe_id, r.name AS recipe_name, r.art AS recipe_art,
                       c.rating, c.notes, c.finished_at
                FROM cooking_records c JOIN recipes r ON r.id=c.recipe_id
                WHERE c.user_id=%s ORDER BY c.finished_at DESC
                """,
                (account_id,),
            )
            rows = cursor.fetchall()
            cursor.close(); connection.close()
            return jsonify(rows)
        payload = request.get_json(silent=True) or {}
        recipe_id = payload.get("recipe_id")
        rating = payload.get("rating")
        notes = payload.get("notes")
        if not recipe_id or (rating is not None and (not isinstance(rating, int) or rating not in range(1, 6))):
            cursor.close(); connection.close()
            return json_error("请提供菜谱 ID，评分需为 1 到 5")
        cursor.execute(
            "INSERT INTO cooking_records (user_id, recipe_id, rating, notes) VALUES (%s, %s, %s, %s)",
            (account_id, recipe_id, rating, notes),
        )
        connection.commit()
        record_id = cursor.lastrowid
        cursor.close(); connection.close()
        return jsonify({"id": record_id, "recipe_id": recipe_id}), 201

    @app.cli.command("init-db")
    def init_database_command():
        run_schema_and_seed()
        print("数据库表与页面种子数据已创建。")

    @app.cli.command("seed-db")
    def seed_database_command():
        seed_catalogue()
        print("页面菜谱与食材数据已更新。")

    @app.cli.command("backup-db")
    def backup_database_command():
        """Create a standalone snapshot for Windows Task Scheduler or manual recovery."""
        connection = connect()
        try:
            backup = create_catalogue_backup(connection, "scheduled", "定时菜谱数据快照", admin_id=None)
            connection.commit()
            print(f"已创建备份：{backup['file_name']}")
        finally:
            connection.close()

    return app


app = create_app()

if __name__ == "__main__":
    config = settings()
    app.run(host=config["flask_host"], port=config["flask_port"], debug=True)
