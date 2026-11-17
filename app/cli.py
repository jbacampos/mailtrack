import argparse
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from html import escape

from PIL import Image, ImageOps

DB = Path("/data/db/mailtrack.sqlite3")
PHOTOS = Path("/data/photos")
THUMBS = Path("/data/thumbnails")
INCOMING = Path("/data/incoming")
# Caminho correspondente no host, mostrado pelo assistente `new`.
INCOMING_HOST = "/var/lib/mailtrack/incoming"
EXPORTS = Path("/data/exports")
# Caminho correspondente no host, usado na transferência via SCP.
EXPORTS_HOST = "/var/lib/mailtrack/exports"

BASE_URL = "https://photos.datalog.top"
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".bmp"}

# Miniatura: arquivo 240x240 (quadrado, corte central) exibido em 72x72 no HTML.
THUMB_PX = 240
THUMB_DISPLAY_PX = 72

VERSION = "0.2.0"


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


def _build_thumbnail(img):
    img = ImageOps.exif_transpose(img)

    # Transparência (PNG/WebP/GIF com alfa) é composta sobre fundo BRANCO:
    # converter RGBA->RGB diretamente descartaria o alfa e deixaria a área
    # transparente escura no JPEG.
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        fundo = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        fundo.alpha_composite(rgba)
        img = fundo

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # Quadrado por corte central, sem distorção; imagens menores são
    # ampliadas para cumprir o tamanho físico de 240x240.
    return ImageOps.fit(
        img,
        (THUMB_PX, THUMB_PX),
        method=Image.LANCZOS,
        centering=(0.5, 0.5)
    )


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


class _Abort(SystemExit):
    """Erro operacional: mensagem clara e código de saída 1.

    `str(exc)` devolve a mensagem (os testes e o operador dependem disso),
    mas o código de saída é 1 (int), e não a string.
    """

    def __init__(self, message):
        super().__init__(1)
        self.message = message

    def __str__(self):
        return self.message


def _fail(message):
    """Interrompe com erro operacional (código 1) e mensagem clara.

    Quem imprime é a borda da CLI (`main`), para que a mensagem apareça
    uma única vez mesmo quando o erro é tratado por um lote.
    """
    raise _Abort(message)


def _resolve_base_url(flag_value):
    """Precedência: --base-url > MAILTRACK_BASE_URL > constante."""
    return (
        flag_value
        or os.getenv("MAILTRACK_BASE_URL")
        or BASE_URL
    ).rstrip("/")


def _parse_timestamp(value):
    if not value:
        return None
    texto = str(value).strip()
    if texto.endswith("Z"):
        texto = texto[:-1] + "+00:00"
    try:
        momento = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento


def _age_seconds(value):
    momento = _parse_timestamp(value)
    if momento is None:
        return None
    decorrido = datetime.now(timezone.utc) - momento
    return max(0, int(decorrido.total_seconds()))


def _relative_age(value):
    segundos = _age_seconds(value)
    if segundos is None:
        return "data desconhecida"
    if segundos < 60:
        return "agora"
    if segundos < 3600:
        minutos = segundos // 60
        return f"há {minutos} minuto" + ("s" if minutos > 1 else "")
    if segundos < 86400:
        horas = segundos // 3600
        return f"há {horas} hora" + ("s" if horas > 1 else "")
    dias = segundos // 86400
    return f"há {dias} dia" + ("s" if dias > 1 else "")


def _file_size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def resolve_message(ref=None):
    """Resolve a mensagem alvo: ID completo ou a última criada.

    Não aceita prefixo, índice nem substring: um ID desconhecido é erro.
    """
    with connect() as db:
        if ref:
            row = db.execute(
                "SELECT id FROM messages WHERE id=?",
                (ref,)
            ).fetchone()
            if row is None:
                _fail(
                    f"Mensagem não encontrada: {ref}. Execute "
                    "'mailtrack list' para ver as mensagens disponíveis."
                )
            return row["id"]

        row = db.execute(
            "SELECT id FROM messages "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()

    if row is None:
        _fail(
            "Nenhuma mensagem cadastrada. Execute "
            "'mailtrack create --to <destinatário> --subject <assunto>' "
            "primeiro."
        )

    return row["id"]


def _looks_like_message_id(value):
    return bool(re.fullmatch(r"MSG-[0-9A-F]{10}", str(value)))


def _collect_files(args):
    arquivos = getattr(args, "files", None)
    if arquivos:
        return [str(arquivo) for arquivo in arquivos]

    unico = getattr(args, "file", None)
    if unico:
        return [str(unico)]

    _fail(
        "Nenhum arquivo informado. "
        "Uso: mailtrack add-photo FOTO [FOTO ...]"
    )


def _validar_limite(limite, permitir_zero):
    """Guarda de segurança para `--limit` fora do argparse.

    O parser já valida (erro de uso, código 2); aqui evitamos que um valor
    negativo chegue ao SQL, onde LIMIT negativo significa "sem limite", e
    produza silenciosamente uma consulta sem restrição.
    """
    if limite is None:
        return None
    if limite < 0:
        raise ValueError(f"limite não pode ser negativo: {limite}")
    if limite == 0 and not permitir_zero:
        raise ValueError("limite deve ser maior que zero")
    return limite


def _create_message(recipient, subject):
    msg_id = "MSG-" + uuid.uuid4().hex[:10].upper()

    with connect() as db:
        db.execute(
            "INSERT INTO messages (id, recipient, subject) VALUES (?, ?, ?)",
            (msg_id, recipient, subject)
        )

    return msg_id


def create_message(args):
    msg_id = _create_message(args.to, args.subject)
    print(f"Mensagem criada: {msg_id}")
    print(f"Destinatário: {args.to}")
    print("Próximo passo: mailtrack add-photo fotos/*.jpg")
    return msg_id


def _add_one(source: Path, message_id: str):
    if not source.is_file():
        _fail(f"Arquivo não encontrado: {source}")

    if source.suffix.lower() not in ALLOWED:
        _fail(
            f"Formato de imagem não permitido: "
            f"{source.suffix or '(sem extensão)'}"
        )

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
                _stage_thumbnail(_build_thumbnail(img), thumb_tmp)

        except Exception as exc:
            raise _Abort(
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
                message_id,
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

    return {
        "original_token": original_token,
        "thumbnail_token": thumbnail_token,
    }


def add_photo(args):
    arquivos = _collect_files(args)

    # Compatibilidade com a sintaxe antiga `add-photo MSG-… foto.jpg`:
    # o primeiro argumento só é tratado como mensagem quando NÃO é um
    # arquivo existente (evita ambiguidade com nomes de arquivo).
    if getattr(args, "message", None) is None and len(arquivos) >= 2:
        primeiro = arquivos[0]
        if _looks_like_message_id(primeiro) and not Path(primeiro).is_file():
            args.message = primeiro
            arquivos = arquivos[1:]

    message_id = resolve_message(getattr(args, "message", None))
    base = _resolve_base_url(getattr(args, "base_url", None))

    # Lote é a única situação em que uma falha não interrompe o comando:
    # com um único arquivo o erro propaga com o tipo original, preservando
    # o contrato do mecanismo D3 (testes T5-T7 e T16).
    em_lote = len(arquivos) > 1
    adicionadas = []
    falharam = []

    for nome in arquivos:
        caminho = Path(nome)
        try:
            resultado = _add_one(caminho, message_id)
        except (SystemExit, Exception) as exc:
            if not em_lote:
                raise
            motivo = (
                str(exc) if isinstance(exc, SystemExit)
                else f"{type(exc).__name__}: {exc}"
            )
            falharam.append((caminho.name, motivo))
            print(f"FALHOU  {caminho.name}: {motivo}", file=sys.stderr)
            continue

        adicionadas.append(caminho.name)
        print(f"OK      {caminho.name}")
        print(f"        miniatura: {base}/t/{resultado['thumbnail_token']}")
        print(f"        original:  {base}/o/{resultado['original_token']}")

    print("")
    print(
        f"Resumo: {len(adicionadas)} adicionada(s), "
        f"{len(falharam)} falha(s) — mensagem {message_id}"
    )

    if em_lote:
        for nome in adicionadas:
            print(f"  adicionada: {nome}")
        for nome, _motivo in falharam:
            print(f"  falhou:     {nome}")

    if falharam:
        raise SystemExit(1)


def list_messages(args):
    limite = _validar_limite(getattr(args, "limit", None), permitir_zero=False)
    como_json = getattr(args, "json", False)

    sql = """
        SELECT m.id, m.recipient, m.subject, m.created_at,
               COUNT(p.id) AS photo_count
        FROM messages m
        LEFT JOIN photos p ON p.message_id = m.id
        GROUP BY m.id
        ORDER BY m.created_at DESC, m.rowid DESC
    """
    parametros = ()
    if limite:
        sql += " LIMIT ?"
        parametros = (limite,)

    with connect() as db:
        rows = db.execute(sql, parametros).fetchall()

    ultima = rows[0]["id"] if rows else None

    if como_json:
        print(json.dumps([
            {
                "id": row["id"],
                "recipient": row["recipient"],
                "subject": row["subject"],
                "created_at": row["created_at"],
                "photo_count": row["photo_count"],
                "age_seconds": _age_seconds(row["created_at"]),
                "is_last": row["id"] == ultima,
            }
            for row in rows
        ], ensure_ascii=False, indent=2))
        return

    if not rows:
        print("Nenhuma mensagem cadastrada.")
        print(
            "Próximo passo: "
            "mailtrack create --to <destinatário> --subject <assunto>"
        )
        return

    for row in rows:
        marcador = "*" if row["id"] == ultima else " "
        print(
            f"{marcador} {row['id']} | {row['recipient']} | "
            f"{row['photo_count']} foto(s) | "
            f"{_relative_age(row['created_at'])} | "
            f"{row['subject'] or '-'}"
        )

    print("")
    print("* = última mensagem")


def show_message(args):
    message_id = resolve_message(getattr(args, "message", None))
    base_url = _resolve_base_url(getattr(args, "base_url", None))
    como_json = getattr(args, "json", False)

    with connect() as db:
        message = db.execute(
            "SELECT * FROM messages WHERE id=?",
            (message_id,)
        ).fetchone()

        photos = db.execute("""
            SELECT * FROM photos
            WHERE message_id=?
            ORDER BY id
        """, (message_id,)).fetchall()

    if message is None:
        _fail(
            f"Mensagem não encontrada: {message_id}. "
            "Execute 'mailtrack list' para ver as mensagens disponíveis."
        )

    fotos = [
        {
            "id": photo["id"],
            "filename": photo["filename"],
            "created_at": photo["created_at"],
            "size_bytes": _file_size(photo["original_path"]),
            "thumbnail_url": f"{base_url}/t/{photo['thumbnail_token']}",
            "original_url": f"{base_url}/o/{photo['original_token']}",
        }
        for photo in photos
    ]

    if como_json:
        print(json.dumps({
            "id": message["id"],
            "recipient": message["recipient"],
            "subject": message["subject"],
            "created_at": message["created_at"],
            "age_seconds": _age_seconds(message["created_at"]),
            "photos": fotos,
        }, ensure_ascii=False, indent=2))
        return

    print(f"ID:           {message['id']}")
    print(f"Destinatário: {message['recipient']}")
    print(f"Assunto:      {message['subject'] or '-'}")
    print(
        f"Criada em:    {message['created_at']} "
        f"({_relative_age(message['created_at'])})"
    )
    print(f"Fotografias:  {len(fotos)}")

    for foto in fotos:
        tamanho = (
            f"{foto['size_bytes']} bytes"
            if foto["size_bytes"] is not None
            else "indisponível"
        )
        print(f"  [{foto['id']}] {foto['filename']}")
        print(f"      data: {foto['created_at']} | tamanho: {tamanho}")
        print(f"      miniatura: {foto['thumbnail_url']}")
        print(f"      original:  {foto['original_url']}")


def export_html(args):
    message_id = resolve_message(getattr(args, "message", None))
    base_url = _resolve_base_url(getattr(args, "base_url", None))

    with connect() as db:
        message = db.execute(
            "SELECT * FROM messages WHERE id=?",
            (message_id,)
        ).fetchone()

        photos = db.execute("""
            SELECT * FROM photos
            WHERE message_id=?
            ORDER BY id
        """, (message_id,)).fetchall()

    if message is None:
        _fail(
            f"Mensagem não encontrada: {message_id}. "
            "Execute 'mailtrack list' para ver as mensagens disponíveis."
        )

    if not photos:
        _fail(
            f"A mensagem {message_id} ainda não possui fotografias. "
            "Execute 'mailtrack add-photo <arquivos>' primeiro."
        )

    gallery_rows = []

    for inicio in range(0, len(photos), 3):
        cells = []

        for photo in photos[inicio:inicio + 3]:
            thumb = f"{base_url}/t/{photo['thumbnail_token']}"
            original = f"{base_url}/o/{photo['original_token']}"

            cells.append(f"""
          <td style="padding:0 10px 10px 0;vertical-align:top;">
            <a href="{escape(original, quote=True)}">
              <img src="{escape(thumb, quote=True)}"
                   alt="{escape(photo['filename'], quote=True)}"
                   width="{THUMB_DISPLAY_PX}" height="{THUMB_DISPLAY_PX}"
                   loading="lazy"
                   style="width:{THUMB_DISPLAY_PX}px;height:{THUMB_DISPLAY_PX}px;object-fit:cover">
            </a>
          </td>""")

        gallery_rows.append(
            "      <tr>" + "".join(cells) + "</tr>"
        )

    gallery = "\n".join(gallery_rows)

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>{escape(message['subject'] or 'Fotografias')}</title>
</head>
<body>
<p>Prezado(a),</p>
<p>Seguem as fotografias:</p>
<p>Atenciosamente,</p>
    <table role="presentation" cellpadding="0" cellspacing="0"
           border="0" align="left" style="border-collapse:collapse;">
{gallery}
    </table>
</body>
</html>
"""

    destino = getattr(args, "output", None)
    if destino:
        output = Path(destino).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
    else:
        EXPORTS.mkdir(parents=True, exist_ok=True)
        output = EXPORTS / f"{message_id}.html"

    tmp = output.parent / f".{output.name}.{secrets.token_hex(16)}.tmp"

    try:
        _stage_html(html, tmp)
        os.replace(tmp, output)
        _fsync_dir(output.parent)
    except BaseException:
        # Apenas o temporário é removido: o destino pode conter a
        # exportação anterior e nunca é apagado por causa de uma falha.
        _discard([tmp])
        raise

    print(f"HTML gerado: {output.resolve()}")
    print(
        "Próximo passo: revise o arquivo e use o conteúdo no e-mail "
        "(as imagens são servidas pelas URLs públicas)."
    )


def _is_single_email(value):
    if not value or value.count("@") != 1:
        return False
    local, dominio = value.split("@")
    if not local or not dominio:
        return False
    if any(char.isspace() for char in value):
        return False
    return "," not in value and ";" not in value


def _ask_input(prompt):
    try:
        return input(prompt)
    except EOFError:
        _fail(
            "Entrada encerrada; operação cancelada. "
            "Nenhuma mensagem foi criada."
        )


def _ask_recipient():
    while True:
        valor = _ask_input("Destinatário: ").strip()
        if _is_single_email(valor):
            return valor
        if not valor:
            print("Informe um destinatário.")
        else:
            print(
                "E-mail inválido. Informe um único endereço de e-mail."
            )


def _ask_subject():
    return _ask_input("Assunto: ").strip()


def _photo_problem(caminho):
    if not caminho.is_file():
        return f"Arquivo não encontrado: {caminho}"
    if caminho.suffix.lower() not in ALLOWED:
        return (
            "Formato de imagem não permitido: "
            f"{caminho.suffix or '(sem extensão)'}"
        )
    try:
        with Image.open(caminho) as img:
            img.verify()
    except Exception as exc:
        return f"Imagem inválida ou não suportada: {exc}"
    return None


def _ask_photos():
    fotos = []
    while True:
        valor = _ask_input(
            "Fotografia (basta o nome do arquivo; Enter para encerrar): "
        ).strip()
        if not valor:
            break

        caminho = Path(valor).expanduser()
        nome_simples = not caminho.is_absolute()
        if nome_simples:
            caminho = INCOMING / caminho

        problema = _photo_problem(caminho)
        if problema:
            if nome_simples and not caminho.is_file():
                print(f"Arquivo não encontrado em {INCOMING}: {valor}")
            else:
                print(problema)
            continue

        fotos.append(str(caminho))
    return fotos


def _print_new_summary(recipient, subject, photos):
    print("")
    print("Resumo:")
    print(f"  Destinatário: {recipient}")
    print(f"  Assunto: {subject or '(sem assunto)'}")
    print(f"  Fotografias: {len(photos)}")
    for indice, caminho in enumerate(photos, 1):
        print(f"    {indice}. {Path(caminho).name}")
    print("")


def _confirm_new():
    resposta = _ask_input(
        "Confirma a criação da mensagem e a geração do HTML? [s/N] "
    ).strip().lower()
    return resposta in {"s", "sim"}


def _photo_count(message_id):
    try:
        with connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS total FROM photos WHERE message_id=?",
                (message_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    return row["total"]


def _photo_count_label(total):
    if total is None:
        return "quantidade de fotografias desconhecida"
    return "1 fotografia" if total == 1 else f"{total} fotografias"


def _photo_progress(total, expected):
    if total is None:
        return "quantidade de fotografias desconhecida"
    return f"{total} de {expected} fotografia(s)"


def _failure_detail(exc):
    if isinstance(exc, (_Abort, OSError, sqlite3.Error)):
        return str(exc).strip()
    return ""


def _discard_empty_message(message_id):
    """Remove apenas uma mensagem sem fotos e sem eventos associados."""
    try:
        with connect() as db:
            cursor = db.execute(
                "DELETE FROM messages WHERE id=? "
                "AND NOT EXISTS ("
                "SELECT 1 FROM photos WHERE message_id=?"
                ") AND NOT EXISTS ("
                "SELECT 1 FROM events WHERE message_id=?"
                ")",
                (message_id, message_id, message_id),
            )
            return cursor.rowcount > 0
    except sqlite3.Error:
        return False


def _run_new(state):
    print("Copie previamente as fotografias para:")
    print(f"{INCOMING_HOST}/")
    print("")
    print(
        "Informe abaixo o nome de cada arquivo. "
        "Pressione ENTER quando terminar."
    )
    print("")

    recipient = _ask_recipient()
    subject = _ask_subject()
    photos = _ask_photos()

    if not photos:
        _fail(
            "Nenhuma fotografia selecionada. Nenhuma mensagem foi criada."
        )

    _print_new_summary(recipient, subject, photos)
    if not _confirm_new():
        print("Operação cancelada. Nenhuma mensagem foi criada.")
        return

    message_id = _create_message(recipient, subject)
    state["message_id"] = message_id
    print(f"Mensagem criada: {message_id}")
    print(f"Adicionando {len(photos)} fotografia(s)...")

    try:
        add_photo(argparse.Namespace(
            files=photos, message=message_id, base_url=None
        ))
    except (SystemExit, OSError, sqlite3.Error) as exc:
        if isinstance(exc, SystemExit) and exc.code in (None, 0):
            raise
        total = _photo_count(message_id)
        detalhe = _failure_detail(exc)
        if total == 0:
            if _discard_empty_message(message_id):
                _fail(
                    "Nenhuma fotografia foi adicionada. A mensagem "
                    "recém-criada foi descartada e o HTML não foi gerado."
                    + (f" Motivo: {detalhe}" if detalhe else "")
                )
            _fail(
                f"Mensagem {message_id} criada sem fotografia. "
                "O HTML não foi gerado. Corrija as falhas acima, complemente "
                f"as fotografias com "
                f"'mailtrack add-photo {message_id} FOTO...' e execute "
                f"'mailtrack export-html {message_id}'."
                + (f" Motivo: {detalhe}" if detalhe else "")
            )
        _fail(
            f"Mensagem {message_id} criada com "
            f"{_photo_progress(total, len(photos))}. O HTML não foi gerado. "
            "Corrija as falhas acima, complete as fotografias com "
            f"'mailtrack add-photo {message_id} FOTO...' e execute "
            f"'mailtrack export-html {message_id}'."
            + (f" Motivo: {detalhe}" if detalhe else "")
        )

    try:
        export_html(argparse.Namespace(
            message=message_id, base_url=None, output=None
        ))
    except (SystemExit, OSError, sqlite3.Error) as exc:
        if isinstance(exc, SystemExit) and exc.code in (None, 0):
            raise
        total = _photo_count(message_id)
        detalhe = _failure_detail(exc)
        _fail(
            f"Mensagem {message_id} criada com "
            f"{_photo_count_label(total)}, mas o HTML não foi gerado."
            + (f" Motivo: {detalhe}" if detalhe else "")
            + f" Execute 'mailtrack export-html {message_id}' "
            "para tentar novamente."
        )

    destino = (EXPORTS / f"{message_id}.html").resolve()
    destino_host = f"{EXPORTS_HOST}/{destino.name}"
    comando_scp = (
        f'scp neo:{destino_host} "$env:USERPROFILE\\Downloads\\"'
    )

    print("")
    print("Mensagem pronta para uso:")
    print(f"  1. HTML gerado no Neo: {destino_host}")
    print("  2. No PowerShell do Windows, execute:")
    print(f"     {comando_scp}")
    print("  3. Após a transferência, abra o arquivo na pasta Downloads,")
    print("     copie o conteúdo renderizado e cole no corpo do e-mail")
    print("     no Gmail, Outlook ou outro provedor.")
    print(f"  4. Envie para {recipient}. O MailTrack não envia o e-mail.")


def new_message(args):
    state = {"message_id": None}
    try:
        _run_new(state)
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        message_id = state["message_id"]
        if message_id is None:
            print(
                "Operação cancelada. Nenhuma mensagem foi criada.",
                file=sys.stderr,
            )
        elif _discard_empty_message(message_id):
            print(
                "Operação cancelada. A mensagem recém-criada foi "
                "descartada porque nenhuma fotografia foi adicionada.",
                file=sys.stderr,
            )
        else:
            print(
                f"Operação interrompida. A mensagem {message_id} pode "
                "estar incompleta; o HTML não foi gerado. Para retomar, "
                f"verifique as fotografias com 'mailtrack show {message_id}', "
                f"complete com 'mailtrack add-photo {message_id} FOTO...' "
                "se necessário e execute "
                f"'mailtrack export-html {message_id}'.",
                file=sys.stderr,
            )
        raise SystemExit(130)


# Rótulos curtos para os tipos de evento registrados pela aplicação.
_EVENT_LABELS = {
    "thumbnail_requested": "Miniatura",
    "original_requested": "Original",
}


def _format_event_time(value):
    """Data e hora do evento no fuso local, em formato compacto."""
    momento = _parse_timestamp(value)
    if momento is None:
        return str(value).strip() if value else None
    return momento.astimezone().strftime("%d/%m/%Y %H:%M:%S")


def _summarize_user_agent(value):
    """Resumo de navegador e sistema, sem despejar o User-Agent completo."""
    if not value:
        return None

    texto = str(value).lower()

    if "edg/" in texto or "edge/" in texto:
        navegador = "Edge"
    elif "opr/" in texto or "opera" in texto:
        navegador = "Opera"
    elif "firefox/" in texto:
        navegador = "Firefox"
    elif "chrome/" in texto or "crios/" in texto:
        navegador = "Chrome"
    elif "safari/" in texto:
        navegador = "Safari"
    elif "curl/" in texto:
        navegador = "curl"
    else:
        navegador = None

    if "windows nt" in texto:
        sistema = "Windows"
    elif "android" in texto:
        sistema = "Android"
    elif "iphone" in texto or "ipad" in texto or "ipod" in texto:
        sistema = "iOS"
    elif "mac os x" in texto or "macintosh" in texto:
        sistema = "macOS"
    elif "linux" in texto or "x11" in texto:
        sistema = "Linux"
    else:
        sistema = None

    partes = [parte for parte in (navegador, sistema) if parte]
    return " / ".join(partes) if partes else None


def _event_label(event_type):
    """Descrição curta do tipo; tipos desconhecidos são preservados."""
    return _EVENT_LABELS.get(event_type, event_type)


def _event_line(row):
    """Uma linha compacta por evento, omitindo campos vazios."""
    ip = row["remote_ip"] or row["client_ip"]
    partes = (
        _format_event_time(row["occurred_at"]),
        row["filename"],
        _event_label(row["event_type"]),
        f"IP {ip}" if ip else None,
        _summarize_user_agent(row["user_agent"]),
    )
    return " | ".join(parte for parte in partes if parte)


def history(args):
    message_id = resolve_message(getattr(args, "message", None))
    limite = _validar_limite(getattr(args, "limit", 20), permitir_zero=True)
    # `--summary` continua aceito por compatibilidade: o formato padrão já
    # é uma linha compacta por evento.

    sql = """
        SELECT e.*, p.filename
        FROM events e
        LEFT JOIN photos p ON p.id = e.photo_id
        WHERE e.message_id = ?
    """
    parametros = [message_id]
    if limite:
        sql += " ORDER BY e.occurred_at DESC, e.id DESC LIMIT ?"
        parametros.append(limite)
    else:
        sql += " ORDER BY e.occurred_at, e.id"

    with connect() as db:
        rows = db.execute(sql, tuple(parametros)).fetchall()
        message = db.execute(
            "SELECT subject FROM messages WHERE id=?",
            (message_id,),
        ).fetchone()

    if not rows:
        print("Nenhum evento registrado.")
        return

    if limite:
        rows = list(reversed(rows))

    print("MAILTRACK — HISTÓRICO DE ACESSOS")
    print(f"Mensagem: {message_id}")
    if message is not None and message["subject"]:
        print(f"Assunto: {message['subject']}")
    print("")

    for row in rows:
        print(_event_line(row))


def _positive_int(value):
    """Tipo do argparse para `list --limit`: inteiro maior que zero."""
    try:
        numero = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"valor inteiro esperado, recebido: {value!r}"
        ) from None
    if numero <= 0:
        raise argparse.ArgumentTypeError(
            f"o limite deve ser um inteiro maior que zero (recebido: {numero})"
        )
    return numero


def _non_negative_int(value):
    """Tipo do argparse para `history --limit`: inteiro maior ou igual a zero."""
    try:
        numero = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"valor inteiro esperado, recebido: {value!r}"
        ) from None
    if numero < 0:
        raise argparse.ArgumentTypeError(
            f"o limite não pode ser negativo (recebido: {numero}); "
            "use 0 para todos os eventos"
        )
    return numero


def _print_short_help():
    print("MAILTRACK — fotografias com histórico de acessos")
    print("")
    print("Uso:")
    print(
        "  mailtrack new                 "
        "assistente interativo (recomendado)"
    )
    print("  mailtrack list                lista as mensagens")
    print(
        "  mailtrack show [MSG]          "
        "mostra uma mensagem e suas fotografias"
    )
    print(
        "  mailtrack history [MSG]       "
        "mostra o histórico de acessos"
    )
    print("")
    print("Comandos manuais:")
    print("  mailtrack create --to EMAIL --subject TEXTO")
    print("  mailtrack add-photo FOTO [FOTO ...]")
    print("  mailtrack export-html [MSG] [-o ARQUIVO]")
    print("")
    print("Início rápido:")
    print("  mailtrack new")
    print("")
    print("Ajuda completa:")
    print("  mailtrack --help")


def main():
    if not sys.argv[1:]:
        _print_short_help()
        return

    parser = argparse.ArgumentParser(
        prog="mailtrack",
        description=(
            "Gerencia mensagens com fotografias rastreadas "
            "(miniaturas, HTML e histórico de acessos)."
        ),
        epilog=(
            "Exemplos:\n"
            "  mailtrack new\n"
            "  mailtrack create --to destinatario@exemplo.com "
            "--subject \"Fotos\"\n"
            "  mailtrack add-photo fotos/*.jpg\n"
            "  mailtrack export-html\n"
            "  mailtrack list\n"
            "  mailtrack show\n"
            "  mailtrack history --limit 5 --summary\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"mailtrack {VERSION}",
    )

    commands = parser.add_subparsers(
        dest="command", required=True, metavar="COMANDO"
    )

    p = commands.add_parser(
        "new",
        help="assistente para criar mensagem, fotos e HTML",
        description=(
            "Assistente interativo: pergunta destinatário, assunto e "
            "fotografias, apresenta um resumo, cria a mensagem, adiciona "
            "as fotografias e gera o HTML."
        ),
        epilog="Exemplo:\n  mailtrack new\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.set_defaults(func=new_message)

    p = commands.add_parser(
        "create",
        help="cria uma mensagem",
        description="Cria uma mensagem para o destinatário informado.",
        epilog=(
            "Exemplo:\n"
            "  mailtrack create --to destinatario@exemplo.com "
            "--subject \"Fotos\"\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--to", required=True, metavar="EMAIL")
    p.add_argument("--subject", default="", metavar="TEXTO")
    p.set_defaults(func=create_message)

    p = commands.add_parser(
        "add-photo",
        help="adiciona uma ou mais fotografias",
        description=(
            "Adiciona fotografias à mensagem. Sem --message, usa a "
            "última mensagem criada."
        ),
        epilog=(
            "Exemplos:\n"
            "  mailtrack add-photo fotos/*.jpg\n"
            "  mailtrack add-photo -m MSG-AB12CD34EF foto1.jpg foto2.jpg\n"
            "  mailtrack add-photo MSG-AB12CD34EF foto.jpg   # forma antiga\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("files", nargs="+", metavar="FOTO")
    p.add_argument("-m", "--message", default=None, metavar="MSG")
    p.add_argument("--base-url", default=None, metavar="URL")
    p.set_defaults(func=add_photo)

    p = commands.add_parser(
        "list",
        help="lista as mensagens",
        description="Lista as mensagens, marcando a última com '*'.",
        epilog="Exemplos:\n  mailtrack list\n  mailtrack list --limit 5\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-n", "--limit", type=_positive_int, default=None,
                   metavar="N")
    p.add_argument("--json", action="store_true", help="saída em JSON")
    p.set_defaults(func=list_messages)

    p = commands.add_parser(
        "show",
        help="mostra uma mensagem e suas fotografias",
        description=(
            "Mostra os dados da mensagem e, por fotografia, data, "
            "tamanho e URLs públicas. Sem MSG, usa a última mensagem."
        ),
        epilog="Exemplo:\n  mailtrack show\n  mailtrack show MSG-AB12CD34EF\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("message", nargs="?", default=None, metavar="MSG")
    p.add_argument("--json", action="store_true", help="saída em JSON")
    p.add_argument("--base-url", default=None, metavar="URL")
    p.set_defaults(func=show_message)

    p = commands.add_parser(
        "export-html",
        help="gera o HTML da mensagem",
        description=(
            "Gera o HTML da mensagem. Sem MSG, usa a última mensagem. "
            "Base URL: --base-url > MAILTRACK_BASE_URL > padrão."
        ),
        epilog=(
            "Exemplos:\n"
            "  mailtrack export-html\n"
            "  mailtrack export-html MSG-AB12CD34EF\n"
            "  mailtrack export-html -o ~/fotos.html\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("message", nargs="?", default=None, metavar="MSG")
    p.add_argument("--base-url", default=None, metavar="URL")
    p.add_argument("-o", "--output", default=None, metavar="ARQUIVO")
    p.set_defaults(func=export_html)

    p = commands.add_parser(
        "history",
        help="mostra os eventos de acesso",
        description=(
            "Mostra os eventos de acesso da mensagem (padrão: os 20 "
            "mais recentes). Sem MSG, usa a última mensagem."
        ),
        epilog=(
            "Exemplos:\n"
            "  mailtrack history\n"
            "  mailtrack history --limit 5 --summary\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("message", nargs="?", default=None, metavar="MSG")
    p.add_argument("-n", "--limit", type=_non_negative_int, default=20,
                   metavar="N")
    p.add_argument(
        "--summary",
        action="store_true",
        help=(
            "mantido por compatibilidade; a saída padrão já é "
            "uma linha compacta por evento"
        ),
    )
    p.set_defaults(func=history)

    args = parser.parse_args()

    try:
        args.func(args)
    except _Abort as exc:
        # Mensagem de erro operacional: uma única vez, no stderr, com
        # código de saída 1 (os erros de uso já saem com 2 pelo argparse).
        print(exc, file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, sqlite3.Error) as exc:
        # Erros operacionais de I/O e de banco: mensagem clara no stderr,
        # código de saída 1, sem traceback. Exceções inesperadas de
        # programação continuam propagando (com traceback) para não
        # esconder defeitos do código.
        print(
            f"Erro operacional ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        print(
            "Verifique permissões de arquivo, espaço em disco e a "
            "disponibilidade do banco de dados.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
