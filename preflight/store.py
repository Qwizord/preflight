"""Хранилище: пользователи, прогоны, находки, обратная связь, инструкции.

SQLite — файл рядом с приложением. Ничего в рабочих папках не создаётся.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

def _data_dir() -> Path:
    """Куда класть данные пользователя.

    В собранном приложении — в профиль пользователя, а НЕ рядом с программой:
    обновление подменяет папку приложения целиком, и база из неё была бы стёрта.
    При запуске из исходников остаётся в проекте — удобнее для разработки.
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        d = base / "Preflight"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(__file__).resolve().parent.parent


DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "preflight.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id        INTEGER PRIMARY KEY,
    login     TEXT UNIQUE NOT NULL,
    name      TEXT NOT NULL,
    role      TEXT NOT NULL DEFAULT 'tech',   -- должность, см. POSITIONS
    salt      TEXT NOT NULL,
    pwd       TEXT NOT NULL,
    created   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY,
    folder    TEXT NOT NULL,
    title     TEXT NOT NULL,
    deadline  TEXT,
    deal_no   TEXT,
    user_id   INTEGER REFERENCES users(id),
    created   TEXT NOT NULL,
    n_stop    INTEGER NOT NULL DEFAULT 0,
    n_warn    INTEGER NOT NULL DEFAULT 0,
    n_note    INTEGER NOT NULL DEFAULT 0,
    payload   TEXT NOT NULL                    -- полный отчёт в JSON
);

CREATE TABLE IF NOT EXISTS findings (
    id        INTEGER PRIMARY KEY,
    run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    code      TEXT NOT NULL,
    severity  INTEGER NOT NULL,
    title     TEXT NOT NULL,
    detail    TEXT,
    files     TEXT,
    verdict   TEXT,                            -- ok | false | missed | NULL
    verdict_by INTEGER REFERENCES users(id),
    verdict_at TEXT,
    comment   TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id        INTEGER PRIMARY KEY,
    scope     TEXT NOT NULL DEFAULT 'global',  -- global | <название сделки>
    body      TEXT NOT NULL,
    user_id   INTEGER REFERENCES users(id),
    updated   TEXT NOT NULL,
    UNIQUE(scope)
);

CREATE TABLE IF NOT EXISTS settings (
    key       TEXT PRIMARY KEY,
    value     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
    id        INTEGER PRIMARY KEY,
    folder    TEXT UNIQUE NOT NULL,
    title     TEXT NOT NULL,
    deadline  TEXT,
    deal_no   TEXT,
    crm_url   TEXT,
    assignee  TEXT,
    reviewer  TEXT,
    status    TEXT NOT NULL DEFAULT 'новая',   -- новая | в работе | на проверке | готово
    difficulty TEXT,
    comment   TEXT,
    updated   TEXT NOT NULL
);
"""


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


# Должности. Ключ хранится в БД, значение показывается людям.
POSITIONS = {
    "director":   "Директор",
    "tech":       "Тех дизайнер",
    "designer":   "Дизайнер",
    "3d":         "3D дизайнер",
    "qc":         "ОТК",
    "manager":    "Менеджер",
    "production": "Производство",
}
# Кому по умолчанию нужна папка со сделками на компьютере.
POSITIONS_WITH_FILES = {"tech", "designer", "3d"}


def position_name(key: str) -> str:
    return POSITIONS.get(key, key)


_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN avatar BLOB",
    "ALTER TABLE users ADD COLUMN avatar_mime TEXT",
    "ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0",
]


def init() -> None:
    with db() as con:
        con.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass          # колонка уже есть
        # старая роль admin переезжает в директора
        con.execute("UPDATE users SET role='director' WHERE role='admin'")


# ── пользователи ────────────────────────────────────────────────────────────

def _hash(pwd: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), 120_000).hex()


def add_user(login: str, name: str, pwd: str, role: str = "tech") -> int:
    salt = secrets.token_hex(16)
    with db() as con:
        cur = con.execute(
            "INSERT INTO users(login,name,role,salt,pwd,created) VALUES(?,?,?,?,?,?)",
            (login.strip().lower(), name.strip(), role, salt, _hash(pwd, salt),
             datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid


def check_user(login: str, pwd: str) -> sqlite3.Row | None:
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE login=?",
                          (login.strip().lower(),)).fetchone()
    if row and secrets.compare_digest(_hash(pwd, row["salt"]), row["pwd"]):
        return row
    return None


def get_user(uid: int) -> sqlite3.Row | None:
    with db() as con:
        return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def users() -> list[sqlite3.Row]:
    with db() as con:
        return con.execute("SELECT * FROM users ORDER BY name").fetchall()


def update_profile(uid: int, *, name: str | None = None, login: str | None = None,
                   role: str | None = None, avatar: bytes | None = None,
                   avatar_mime: str | None = None, clear_avatar: bool = False) -> None:
    sets, vals = [], []
    if name:
        sets.append("name=?"); vals.append(name.strip())
    if login:
        sets.append("login=?"); vals.append(login.strip().lower())
    if role:
        sets.append("role=?"); vals.append(role)
    if clear_avatar:
        sets += ["avatar=NULL", "avatar_mime=NULL"]
    elif avatar:
        sets += ["avatar=?", "avatar_mime=?"]; vals += [avatar, avatar_mime]
    if not sets:
        return
    vals.append(uid)
    with db() as con:
        con.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)


def set_password(uid: int, pwd: str) -> None:
    salt = secrets.token_hex(16)
    with db() as con:
        con.execute("UPDATE users SET salt=?, pwd=? WHERE id=?", (salt, _hash(pwd, salt), uid))


def mark_onboarded(uid: int) -> None:
    with db() as con:
        con.execute("UPDATE users SET onboarded=1 WHERE id=?", (uid,))


def any_users() -> bool:
    with db() as con:
        return con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


# ── прогоны и находки ───────────────────────────────────────────────────────

def save_run(*, folder: str, title: str, deadline: str | None, deal_no: str | None,
             user_id: int | None, findings: list[dict], payload: dict) -> int:
    counts = {3: 0, 2: 0, 1: 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    with db() as con:
        cur = con.execute(
            """INSERT INTO runs(folder,title,deadline,deal_no,user_id,created,
                                n_stop,n_warn,n_note,payload)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (folder, title, deadline, deal_no, user_id,
             datetime.now().isoformat(timespec="seconds"),
             counts[3], counts[2], counts[1], json.dumps(payload, ensure_ascii=False)))
        run_id = cur.lastrowid
        for f in findings:
            con.execute(
                """INSERT INTO findings(run_id,code,severity,title,detail,files)
                   VALUES(?,?,?,?,?,?)""",
                (run_id, f["code"], f["severity"], f["title"],
                 f.get("detail", ""), json.dumps(f.get("files", []), ensure_ascii=False)))
    return run_id


def get_run(run_id: int) -> sqlite3.Row | None:
    with db() as con:
        return con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def run_findings(run_id: int) -> list[sqlite3.Row]:
    with db() as con:
        return con.execute(
            "SELECT * FROM findings WHERE run_id=? ORDER BY severity DESC, id",
            (run_id,)).fetchall()


def recent_runs(limit: int = 60) -> list[sqlite3.Row]:
    with db() as con:
        return con.execute(
            """SELECT r.*, u.name AS user_name FROM runs r
               LEFT JOIN users u ON u.id=r.user_id
               ORDER BY r.id DESC LIMIT ?""", (limit,)).fetchall()


def last_run_for(folder: str) -> sqlite3.Row | None:
    with db() as con:
        return con.execute(
            "SELECT * FROM runs WHERE folder=? ORDER BY id DESC LIMIT 1",
            (folder,)).fetchone()


def set_verdict(finding_id: int, verdict: str, user_id: int,
                comment: str | None = None) -> None:
    with db() as con:
        con.execute(
            """UPDATE findings SET verdict=?, verdict_by=?, verdict_at=?, comment=?
               WHERE id=?""",
            (verdict, user_id, datetime.now().isoformat(timespec="seconds"),
             comment, finding_id))


def rule_stats() -> list[sqlite3.Row]:
    """Сколько раз правило сработало и как его оценили — основа калибровки."""
    with db() as con:
        return con.execute(
            """SELECT code,
                      COUNT(*)                                        AS total,
                      SUM(verdict='ok')                               AS ok,
                      SUM(verdict='false')                            AS wrong,
                      SUM(verdict IS NULL)                            AS pending
               FROM findings GROUP BY code ORDER BY total DESC""").fetchall()


# ── инструкции ──────────────────────────────────────────────────────────────

def get_note(scope: str = "global") -> str:
    with db() as con:
        row = con.execute("SELECT body FROM notes WHERE scope=?", (scope,)).fetchone()
    return row["body"] if row else ""


def set_note(scope: str, body: str, user_id: int | None) -> None:
    with db() as con:
        con.execute(
            """INSERT INTO notes(scope,body,user_id,updated) VALUES(?,?,?,?)
               ON CONFLICT(scope) DO UPDATE SET body=excluded.body,
                   user_id=excluded.user_id, updated=excluded.updated""",
            (scope, body, user_id, datetime.now().isoformat(timespec="seconds")))


# ── учёт сделок ─────────────────────────────────────────────────────────────

DEAL_FIELDS = ("title", "deadline", "deal_no", "crm_url", "assignee",
               "reviewer", "status", "difficulty", "comment")


def upsert_deal(folder: str, **kw) -> None:
    fields = {k: v for k, v in kw.items() if k in DEAL_FIELDS}
    now = datetime.now().isoformat(timespec="seconds")
    with db() as con:
        row = con.execute("SELECT id FROM deals WHERE folder=?", (folder,)).fetchone()
        if row:
            if fields:
                sets = ",".join(f"{k}=?" for k in fields)
                con.execute(f"UPDATE deals SET {sets}, updated=? WHERE folder=?",
                            (*fields.values(), now, folder))
        else:
            cols = ["folder", *fields, "updated"]
            con.execute(
                f"INSERT INTO deals({','.join(cols)}) "
                f"VALUES({','.join('?' * len(cols))})",
                (folder, *fields.values(), now))


def get_deal(folder: str) -> sqlite3.Row | None:
    with db() as con:
        return con.execute("SELECT * FROM deals WHERE folder=?", (folder,)).fetchone()


def deals() -> list[sqlite3.Row]:
    with db() as con:
        return con.execute("SELECT * FROM deals ORDER BY deadline, title").fetchall()


# ── настройки ───────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as con:
        con.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (key, value))


# ── статистика ──────────────────────────────────────────────────────────────

def stats() -> dict:
    """Сводка для страницы статистики."""
    with db() as con:
        tot = con.execute(
            """SELECT COUNT(*) n, SUM(n_stop) st, SUM(n_warn) wa, SUM(n_note) no,
                      SUM(n_stop=0 AND n_warn=0 AND n_note=0) clean,
                      COUNT(DISTINCT folder) deals
               FROM runs""").fetchone()
        by_day = con.execute(
            """SELECT substr(created,1,10) d, COUNT(*) n,
                      SUM(n_stop) st, SUM(n_warn) wa
               FROM runs GROUP BY d ORDER BY d DESC LIMIT 14""").fetchall()
        by_user = con.execute(
            """SELECT u.name, COUNT(*) n, SUM(r.n_stop) st, SUM(r.n_warn) wa,
                      SUM(r.n_stop=0 AND r.n_warn=0 AND r.n_note=0) clean
               FROM runs r LEFT JOIN users u ON u.id=r.user_id
               GROUP BY r.user_id ORDER BY n DESC""").fetchall()
        top = con.execute(
            """SELECT code, COUNT(*) n, SUM(verdict='ok') ok, SUM(verdict='false') wrong
               FROM findings GROUP BY code ORDER BY n DESC LIMIT 10""").fetchall()
        verdicts = con.execute(
            """SELECT SUM(verdict='ok') ok, SUM(verdict='false') wrong,
                      SUM(verdict='missed') missed, SUM(verdict IS NULL) pending
               FROM findings""").fetchone()
    return {"total": tot, "by_day": by_day, "by_user": by_user,
            "top": top, "verdicts": verdicts}


def db_path() -> Path:
    return DB_PATH


def root_dir() -> Path:
    """Корень с папками сделок.

    Приоритет: заданное в интерфейсе → PREFLIGHT_ROOT → путь по умолчанию.
    """
    try:
        saved = get_setting("root_dir")
    except Exception:
        saved = ""
    if saved:
        return Path(saved).expanduser()
    if env := os.environ.get("PREFLIGHT_ROOT"):
        return Path(env).expanduser()
    return Path.home() / "Desktop" / "Medali Store"
