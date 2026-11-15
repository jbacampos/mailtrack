import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

DB_PATH = Path(os.getenv("MAILTRACK_DB", "/data/db/mailtrack.sqlite3"))
PHOTO_DIR = Path(os.getenv("MAILTRACK_PHOTOS", "/data/photos"))
THUMB_DIR = Path(os.getenv("MAILTRACK_THUMBNAILS", "/data/thumbnails"))


@contextmanager
def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    with connect_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            recipient TEXT NOT NULL,
            subject TEXT,
            created_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );

        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            original_token TEXT NOT NULL UNIQUE,
            thumbnail_token TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            original_path TEXT NOT NULL,
            thumbnail_path TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            FOREIGN KEY(message_id) REFERENCES messages(id)
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            photo_id INTEGER,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            remote_ip TEXT,
            client_ip TEXT,
            user_agent TEXT,
            FOREIGN KEY(message_id) REFERENCES messages(id),
            FOREIGN KEY(photo_id) REFERENCES photos(id)
        );

        CREATE INDEX IF NOT EXISTS idx_events_message
            ON events(message_id, occurred_at);

        CREATE INDEX IF NOT EXISTS idx_events_photo
            ON events(photo_id, occurred_at);
        """)

        # Migração incremental: preserva os eventos existentes.
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(events)")
        }

        migrations = {
            "cf_ray": "TEXT",
            "cf_country": "TEXT",
            "forwarded_for": "TEXT",
            "referer": "TEXT",
        }

        for name, sql_type in migrations.items():
            if name not in columns:
                db.execute(
                    f"ALTER TABLE events ADD COLUMN {name} {sql_type}"
                )


def record_event(request: Request, photo, event_type: str):
    # Cloudflare fornece CF-Connecting-IP.
    # Mantemos também o IP da conexão recebida pelo aplicativo.
    remote_ip = request.headers.get("cf-connecting-ip")
    client_ip = (
        request.client.host if request.client else None
    )
    user_agent = request.headers.get("user-agent")
    cf_ray = request.headers.get("cf-ray")
    cf_country = request.headers.get("cf-ipcountry")
    forwarded_for = request.headers.get("x-forwarded-for")
    referer = request.headers.get("referer")

    with connect_db() as db:
        db.execute("""
            INSERT INTO events (
                message_id, photo_id, event_type,
                remote_ip, client_ip, user_agent,
                cf_ray, cf_country, forwarded_for, referer
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            photo["message_id"],
            photo["id"],
            event_type,
            remote_ip,
            client_ip,
            user_agent,
            cf_ray,
            cf_country,
            forwarded_for,
            referer
        ))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="MailTrack",
    version="0.1.0",
    lifespan=lifespan
)


@app.get("/health")
def health():
    return {"status": "ok", "application": "MailTrack"}


def get_photo(token: str, token_field: str):
    if token_field not in ("original_token", "thumbnail_token"):
        raise ValueError("Invalid token field")

    with connect_db() as db:
        photo = db.execute(
            f"SELECT * FROM photos WHERE {token_field} = ?",
            (token,)
        ).fetchone()

    if photo is None:
        raise HTTPException(status_code=404, detail="Image not found")

    return photo


@app.get("/t/{token}")
def get_thumbnail(token: str, request: Request):
    photo = get_photo(token, "thumbnail_token")
    path = Path(photo["thumbnail_path"])

    if not path.is_file():
        raise HTTPException(status_code=404, detail="Thumbnail unavailable")

    record_event(request, photo, "thumbnail_requested")

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"}
    )


@app.get("/o/{token}")
def get_original(token: str, request: Request):
    photo = get_photo(token, "original_token")
    path = Path(photo["original_path"])

    if not path.is_file():
        raise HTTPException(status_code=404, detail="Original unavailable")

    record_event(request, photo, "original_requested")

    return FileResponse(
        path,
        filename=photo["filename"],
        content_disposition_type="inline",
        headers={"Cache-Control": "no-store"}
    )
