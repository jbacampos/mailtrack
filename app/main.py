import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

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


LANDING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Thumbmails — Photos made easy to share</title>
  <meta name="description" content="Thumbmails is a photo-sharing service that makes it easy to include photographs in email messages without attaching large files.">
  <meta name="theme-color" content="#f5f7f9">
  <style>
    :root {
      color-scheme: light;
      --background: #f5f7f9;
      --surface: #ffffff;
      --text: #1f2933;
      --muted: #5b6773;
      --accent: #2f6f8f;
      --accent-soft: #e8f0f4;
      --border: #e3e7eb;
    }

    * {
      box-sizing: border-box;
    }

    html {
      -webkit-text-size-adjust: 100%;
    }

    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 2.5rem 1.25rem;
      background: var(--background);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
      font-size: 1rem;
      line-height: 1.65;
    }

    main {
      width: 100%;
      max-width: 40rem;
      padding: clamp(2rem, 6vw, 3.5rem);
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 1rem;
      box-shadow: 0 1px 2px rgba(31, 41, 51, 0.04),
        0 16px 40px rgba(31, 41, 51, 0.06);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 1.75rem;
    }

    .mark {
      display: grid;
      grid-template-columns: repeat(2, 0.5rem);
      grid-template-rows: repeat(2, 0.5rem);
      gap: 0.1875rem;
      padding: 0.375rem;
      border-radius: 0.5rem;
      background: var(--accent-soft);
      flex: none;
    }

    .mark span {
      display: block;
      border-radius: 0.125rem;
      background: var(--accent);
    }

    .mark span:nth-child(2),
    .mark span:nth-child(3) {
      opacity: 0.55;
    }

    h1 {
      margin: 0;
      font-size: clamp(1.5rem, 4vw, 1.875rem);
      font-weight: 700;
      letter-spacing: -0.02em;
      line-height: 1.2;
    }

    .tagline {
      margin: 0 0 1rem;
      font-size: clamp(1.125rem, 3vw, 1.375rem);
      font-weight: 600;
      letter-spacing: -0.01em;
    }

    .lead,
    .about {
      margin: 0;
      color: var(--muted);
    }

    .lead {
      font-size: 1.0625rem;
    }

    .promise {
      margin: 1.75rem 0;
      padding: 0.875rem 1rem;
      border-left: 3px solid var(--accent);
      background: var(--accent-soft);
      border-radius: 0 0.375rem 0.375rem 0;
      font-weight: 600;
    }

    .about {
      font-size: 0.9375rem;
    }

    @media (max-width: 30rem) {
      body {
        padding: 1rem;
        align-items: flex-start;
      }

      main {
        padding: 1.75rem 1.25rem;
        border-radius: 0.75rem;
      }
    }
  </style>
</head>
<body>
  <main>
    <header class="brand">
      <span class="mark" aria-hidden="true">
        <span></span>
        <span></span>
        <span></span>
        <span></span>
      </span>
      <h1>Thumbmails</h1>
    </header>
    <p class="tagline">Photos made easy to share.</p>
    <p class="lead">Share your photos through beautiful, clickable
      thumbnails. Recipients can view the original images with a single
      click.</p>
    <p class="promise">Simple. Direct. No attachments required.</p>
    <p class="about">Thumbmails is a photo-sharing service that makes it
      easy to include photographs in email messages without attaching
      large files.</p>
  </main>
</body>
</html>
"""


@app.get("/")
def landing_page():
    return HTMLResponse(content=LANDING_PAGE)


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
