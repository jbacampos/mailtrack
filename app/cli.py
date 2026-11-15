import argparse
import secrets
import shutil
import sqlite3
import uuid
from pathlib import Path
from html import escape

from PIL import Image, ImageOps

DB = Path("/data/db/mailtrack.sqlite3")
PHOTOS = Path("/data/photos")
THUMBS = Path("/data/thumbnails")
INCOMING = Path("/data/incoming")
EXPORTS = Path("/data/exports")

BASE_URL = "https://photos.datalog.top"
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".bmp"}


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def create_message(args):
    msg_id = "MSG-" + uuid.uuid4().hex[:10].upper()

    with connect() as db:
        db.execute(
            "INSERT INTO messages (id, recipient, subject) VALUES (?, ?, ?)",
            (msg_id, args.to, args.subject)
        )

    print(f"Mensagem criada: {msg_id}")
    print(f"Destinatário: {args.to}")


def add_photo(args):
    source = Path(args.file)

    if not source.is_file():
        raise SystemExit(f"Arquivo não encontrado: {source}")

    if source.suffix.lower() not in ALLOWED:
        raise SystemExit("Formato de imagem não permitido.")

    with connect() as db:
        message = db.execute(
            "SELECT id FROM messages WHERE id=?",
            (args.message,)
        ).fetchone()

    if message is None:
        raise SystemExit("Mensagem não encontrada.")

    original_token = secrets.token_urlsafe(32)
    thumbnail_token = secrets.token_urlsafe(32)

    try:
        with Image.open(source) as img:
            img.verify()

        with Image.open(source) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((480, 480))

            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            thumb_name = thumbnail_token + ".jpg"
            thumb_path = THUMBS / thumb_name
            img.save(thumb_path, "JPEG", quality=82, optimize=True)

    except Exception as exc:
        raise SystemExit(f"Imagem inválida ou não suportada: {exc}")

    original_name = original_token + source.suffix.lower()
    original_path = PHOTOS / original_name

    shutil.copy2(source, original_path)

    try:
        with connect() as db:
            db.execute("""
                INSERT INTO photos (
                    message_id, original_token, thumbnail_token,
                    filename, original_path, thumbnail_path
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                args.message,
                original_token,
                thumbnail_token,
                source.name,
                str(original_path),
                str(thumb_path)
            ))
    except Exception:
        original_path.unlink(missing_ok=True)
        thumb_path.unlink(missing_ok=True)
        raise

    print(f"Fotografia adicionada: {source.name}")
    print(f"Miniatura: {thumb_path.name}")


def list_messages(args):
    with connect() as db:
        rows = db.execute("""
            SELECT m.id, m.recipient, m.subject, m.created_at,
                   COUNT(p.id) AS photo_count
            FROM messages m
            LEFT JOIN photos p ON p.message_id=m.id
            GROUP BY m.id
            ORDER BY m.created_at DESC
        """).fetchall()

    if not rows:
        print("Nenhuma mensagem cadastrada.")
        return

    for row in rows:
        print(
            f"{row['id']} | {row['recipient']} | "
            f"{row['photo_count']} foto(s) | {row['subject'] or '-'}"
        )


def show_message(args):
    with connect() as db:
        message = db.execute(
            "SELECT * FROM messages WHERE id=?",
            (args.message,)
        ).fetchone()

        photos = db.execute("""
            SELECT id, filename, created_at
            FROM photos WHERE message_id=?
            ORDER BY id
        """, (args.message,)).fetchall()

    if message is None:
        raise SystemExit("Mensagem não encontrada.")

    print(f"ID:           {message['id']}")
    print(f"Destinatário: {message['recipient']}")
    print(f"Assunto:      {message['subject'] or '-'}")
    print(f"Criada em:    {message['created_at']}")
    print(f"Fotografias:  {len(photos)}")

    for photo in photos:
        print(f"  [{photo['id']}] {photo['filename']}")


def export_html(args):
    with connect() as db:
        message = db.execute(
            "SELECT * FROM messages WHERE id=?",
            (args.message,)
        ).fetchone()

        photos = db.execute("""
            SELECT * FROM photos
            WHERE message_id=?
            ORDER BY id
        """, (args.message,)).fetchall()

    if message is None:
        raise SystemExit("Mensagem não encontrada.")

    if not photos:
        raise SystemExit("A mensagem ainda não possui fotografias.")

    blocks = []

    for photo in photos:
        thumb = f"{args.base_url.rstrip('/')}/t/{photo['thumbnail_token']}"
        original = f"{args.base_url.rstrip('/')}/o/{photo['original_token']}"

        blocks.append(f"""
        <p>
          <a href="{escape(original, quote=True)}">
            <img src="{escape(thumb, quote=True)}"
                 alt="{escape(photo['filename'], quote=True)}"
                 style="max-width:120px;height:auto">
          </a>
        </p>
        """)

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>{escape(message['subject'] or 'Fotografias')}</title>
</head>
<body>
<p>Prezado(a),</p>
<p>Seguem as fotografias:</p>
{''.join(blocks)}
<p>Atenciosamente,</p>
</body>
</html>
"""

    EXPORTS.mkdir(parents=True, exist_ok=True)
    output = EXPORTS / f"{args.message}.html"
    output.write_text(html, encoding="utf-8")

    print(f"HTML gerado: {output}")


def history(args):
    with connect() as db:
        rows = db.execute("""
            SELECT e.*, p.filename
            FROM events e
            LEFT JOIN photos p ON p.id=e.photo_id
            WHERE e.message_id=?
            ORDER BY e.occurred_at, e.id
        """, (args.message,)).fetchall()

    if not rows:
        print("Nenhum evento registrado.")
        return

    for row in rows:
        print(
            f"{row['occurred_at']} | "
            f"{row['event_type']} | "
            f"{row['filename'] or '-'}"
        )

        print(f"  IP visitante (Cloudflare): {row['remote_ip'] or '-'}")
        print(f"  IP da conexão:             {row['client_ip'] or '-'}")
        print(f"  Cloudflare Ray:            {row['cf_ray'] or '-'}")
        print(f"  País informado:            {row['cf_country'] or '-'}")
        print(f"  X-Forwarded-For:           {row['forwarded_for'] or '-'}")
        print(f"  Referer:                   {row['referer'] or '-'}")
        print(f"  User-Agent:                {row['user_agent'] or '-'}")


def main():
    parser = argparse.ArgumentParser(prog="mailtrack")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("create")
    p.add_argument("--to", required=True)
    p.add_argument("--subject", default="")
    p.set_defaults(func=create_message)

    p = commands.add_parser("add-photo")
    p.add_argument("message")
    p.add_argument("file")
    p.set_defaults(func=add_photo)

    p = commands.add_parser("list")
    p.set_defaults(func=list_messages)

    p = commands.add_parser("show")
    p.add_argument("message")
    p.set_defaults(func=show_message)

    p = commands.add_parser("export-html")
    p.add_argument("message")
    p.add_argument("--base-url", default=BASE_URL)
    p.set_defaults(func=export_html)

    p = commands.add_parser("history")
    p.add_argument("message")
    p.set_defaults(func=history)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
