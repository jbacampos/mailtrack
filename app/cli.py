import argparse
import os
import secrets
import shutil
import sqlite3
import sys
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


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stage_thumbnail(img, destination: Path) -> None:
    with open(destination, "xb") as out:
        img.save(out, "JPEG", quality=82, optimize=True)
        out.flush()
        os.fsync(out.fileno())


def _stage_original(source: Path, destination: Path) -> None:
    with open(destination, "xb") as out, source.open("rb") as inp:
        shutil.copyfileobj(inp, out, 1024 * 1024)
        out.flush()
        shutil.copystat(source, destination)
        os.fsync(out.fileno())


def _stage_html(content: str, destination: Path) -> None:
    with open(destination, "xb") as out:
        out.write(content.encode("utf-8"))
        out.flush()
        os.fsync(out.fileno())


def _photo_exists(original_token: str, thumbnail_token: str) -> bool:
    try:
        db = connect()
        try:
            row = db.execute(
                "SELECT 1 FROM photos "
                "WHERE original_token=? OR thumbnail_token=?",
                (original_token, thumbnail_token)
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return True

    return row is not None


def _discard(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(
                f"Aviso: não foi possível remover {path}: {exc}",
                file=sys.stderr
            )


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

    original_name = original_token + source.suffix.lower()
    original_path = PHOTOS / original_name
    thumb_name = thumbnail_token + ".jpg"
    thumb_path = THUMBS / thumb_name

    # Os temporários ficam no mesmo diretório do destino: os.replace()
    # só é atômico dentro do mesmo sistema de arquivos.
    original_tmp = PHOTOS / f".{original_name}.{secrets.token_hex(16)}.tmp"
    thumb_tmp = THUMBS / f".{thumb_name}.{secrets.token_hex(16)}.tmp"

    # Os destinos entram na lista de limpeza ANTES do os.replace(): uma
    # interrupção logo após o rename não deixa arquivo órfão. Nomes usam
    # tokens novos (128 bits), então não há arquivo alheio nesses caminhos.
    artifacts = [thumb_tmp, thumb_path, original_tmp, original_path]

    committed = False
    db_write_started = False

    try:
        try:
            with Image.open(source) as img:
                img.verify()

            with Image.open(source) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((480, 480))

                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")

                _stage_thumbnail(img, thumb_tmp)

        except Exception as exc:
            raise SystemExit(
                f"Imagem inválida ou não suportada: {exc}"
            ) from exc

        os.replace(thumb_tmp, thumb_path)
        _fsync_dir(THUMBS)

        _stage_original(source, original_tmp)
        os.replace(original_tmp, original_path)
        _fsync_dir(PHOTOS)

        db = connect()
        db_write_started = True
        try:
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
            db.commit()
            committed = True
        finally:
            db.close()

    except BaseException:
        # close() já desfez a transação quando o commit não ocorreu. Se o
        # commit foi confirmado (inclusive quando a interrupção chega logo
        # depois dele), apagar os arquivos deixaria uma linha apontando
        # para arquivo inexistente: nesse caso eles são preservados.
        #
        # Antes da etapa de banco não existe linha que justifique manter
        # os arquivos: apagar direto evita temporários abandonados quando
        # o SQLite está indisponível (_photo_exists responderia True).
        if not db_write_started:
            _discard(artifacts)
        elif not committed and not _photo_exists(
            original_token, thumbnail_token
        ):
            _discard(artifacts)
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
    tmp = EXPORTS / f".{output.name}.{secrets.token_hex(16)}.tmp"

    try:
        _stage_html(html, tmp)
        os.replace(tmp, output)
        _fsync_dir(EXPORTS)
    except BaseException:
        # Apenas o temporário é removido: o destino pode conter a
        # exportação anterior e nunca é apagado por causa de uma falha.
        _discard([tmp])
        raise

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
