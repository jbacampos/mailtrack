"""Testes da CLI do MailTrack: publicacao atomica (proposta D3)."""

import contextlib
import errno
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import cli  # noqa: E402


SCHEMA = """
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
    cf_ray TEXT,
    cf_country TEXT,
    forwarded_for TEXT,
    referer TEXT,
    FOREIGN KEY(message_id) REFERENCES messages(id),
    FOREIGN KEY(photo_id) REFERENCES photos(id)
);
"""

TMP_NAME = re.compile(r"^\..+\.[0-9a-f]{32}\.tmp$")


class ProxyConnection:
    """Embrulha a conexao para interceptar o momento do commit."""

    def __init__(self, connection, on_commit=None):
        self._connection = connection
        self._on_commit = on_commit

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._connection.__exit__(*exc_info)

    def commit(self):
        if self._on_commit is None:
            return self._connection.commit()
        return self._on_commit(self._connection)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class CliTestCase(unittest.TestCase):
    """Base com banco, diretorios e imagem temporarios."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.base = Path(self._tmpdir.name)

        self.photos = self.base / "photos"
        self.thumbs = self.base / "thumbs"
        self.exports = self.base / "exports"
        for directory in (self.photos, self.thumbs, self.exports):
            directory.mkdir()

        self.db_path = self.base / "db" / "mailtrack.sqlite3"
        self.db_path.parent.mkdir()

        self.message_id = "MSG-TEST"
        self.subject = "Fotos & Cia"

        for target, value in (
            ("DB", self.db_path),
            ("PHOTOS", self.photos),
            ("THUMBS", self.thumbs),
            ("EXPORTS", self.exports),
        ):
            patcher = mock.patch.object(cli, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        with self._connect() as db:
            db.executescript(SCHEMA)
            db.execute(
                "INSERT INTO messages (id, recipient, subject) "
                "VALUES (?, ?, ?)",
                (self.message_id, "dest@example.com", self.subject)
            )

        self.source = self.base / "in" / "foto.jpg"
        self.source.parent.mkdir()
        Image.new("RGB", (1600, 1200), (10, 20, 30)).save(
            self.source, "JPEG", quality=90
        )

    def _connect(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _photo_args(self, path=None):
        return types.SimpleNamespace(
            message=self.message_id,
            file=str(path or self.source)
        )

    def _export_args(self, message=None, base_url="https://photos.example"):
        return types.SimpleNamespace(
            message=message or self.message_id,
            base_url=base_url
        )

    def _write_image(self, name, size, mode="RGB", color=(10, 20, 30)):
        path = self.base / "in" / name
        Image.new(mode, size, color).save(path)
        return path

    def _photo_row(self):
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM photos ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def _nova_mensagem(self, msg_id, quando, recipient="outro@example.com",
                       subject="Nova"):
        with self._connect() as db:
            db.execute(
                "INSERT INTO messages (id, recipient, subject, created_at) "
                "VALUES (?, ?, ?, ?)",
                (msg_id, recipient, subject, quando)
            )

    def _insert_photo(self, filename, original, thumb, message_id=None):
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO photos (message_id, original_token, "
                "thumbnail_token, filename, original_path, "
                "thumbnail_path) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message_id or self.message_id,
                    original,
                    thumb,
                    filename,
                    f"/data/photos/{original}.jpg",
                    f"/data/thumbnails/{thumb}.jpg",
                ),
            )
            return cursor.lastrowid

    def _insert_event(
        self,
        event_type,
        occurred_at,
        photo_id=None,
        remote_ip=None,
        client_ip=None,
        user_agent=None,
        message_id=None,
    ):
        with self._connect() as db:
            db.execute(
                "INSERT INTO events (message_id, photo_id, event_type, "
                "occurred_at, remote_ip, client_ip, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id or self.message_id,
                    photo_id,
                    event_type,
                    occurred_at,
                    remote_ip,
                    client_ip,
                    user_agent,
                ),
            )

    @staticmethod
    def _event_lines(texto):
        return [linha for linha in texto.splitlines() if " | " in linha]

    def _main(self, *argv):
        with mock.patch.object(sys, "argv", ["mailtrack", *argv]):
            return cli.main()

    def _add_photo_rows(
        self, rows=(("ORIG1", "THUMB1"), ("ORIG2", "THUMB2"))
    ):
        with self._connect() as db:
            for original, thumb in rows:
                db.execute(
                    "INSERT INTO photos (message_id, original_token, "
                    "thumbnail_token, filename, original_path, "
                    "thumbnail_path) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.message_id,
                        original,
                        thumb,
                        f"{original}.jpg",
                        f"/data/photos/{original}.jpg",
                        f"/data/thumbnails/{thumb}.jpg",
                    )
                )

    def _names(self, directory):
        return sorted(path.name for path in Path(directory).iterdir())

    def _temps(self, directory):
        return [
            name for name in self._names(directory)
            if name.endswith(".tmp")
        ]

    def _photo_count(self):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]

    def _reset(self):
        for directory in (self.photos, self.thumbs, self.exports):
            for path in Path(directory).iterdir():
                path.unlink()

    def _reset_all(self):
        self._reset()
        with self._connect() as db:
            db.execute("DELETE FROM photos")

    def _install_insert_trigger(self):
        with self._connect() as db:
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS no_insert "
                "BEFORE INSERT ON photos "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
        self.addCleanup(self._drop_insert_trigger)

    def _drop_insert_trigger(self):
        with self._connect() as db:
            db.execute("DROP TRIGGER IF EXISTS no_insert")

    def _armed_factory(self, arm_at, on_commit):
        real_connect = cli.connect
        state = {"n": 0}

        def factory():
            state["n"] += 1
            hook = on_commit if state["n"] == arm_at else None
            return ProxyConnection(real_connect(), hook)

        return factory


class AddPhotoTest(CliTestCase):
    """T1-T16, T21-T26: publicacao de miniaturas e originais."""

    def test_t1_add_photo_publica_original_e_miniatura(self):
        cli.add_photo(self._photo_args())

        self.assertEqual(self._photo_count(), 1)
        with self._connect() as db:
            row = db.execute("SELECT * FROM photos").fetchone()

        original = Path(row["original_path"])
        thumb = Path(row["thumbnail_path"])
        self.assertEqual(original.parent, self.photos)
        self.assertEqual(thumb.parent, self.thumbs)
        self.assertEqual(row["filename"], self.source.name)
        self.assertEqual(original.read_bytes(), self.source.read_bytes())
        self.assertEqual(
            stat.S_IMODE(original.stat().st_mode),
            stat.S_IMODE(self.source.stat().st_mode),
        )
        self.assertEqual(
            original.stat().st_mtime_ns, self.source.stat().st_mtime_ns
        )
        with Image.open(thumb) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (240, 240))

    def test_t2_nao_deixa_temporarios_apos_sucesso(self):
        cli.add_photo(self._photo_args())

        self.assertEqual(self._temps(self.photos), [])
        self.assertEqual(self._temps(self.thumbs), [])
        self.assertEqual(len(self._names(self.photos)), 1)
        self.assertEqual(len(self._names(self.thumbs)), 1)

    def test_t3_temporarios_usam_token_hex_16_no_diretorio_de_destino(self):
        chamadas = []
        destinos = []
        real_hex = cli.secrets.token_hex
        real_thumb = cli._stage_thumbnail
        real_original = cli._stage_original

        def spy_hex(tamanho):
            chamadas.append(tamanho)
            return real_hex(tamanho)

        def spy_thumb(img, destination):
            destinos.append(Path(destination))
            return real_thumb(img, destination)

        def spy_original(source, destination):
            destinos.append(Path(destination))
            return real_original(source, destination)

        with mock.patch.object(cli.secrets, "token_hex", spy_hex), \
                mock.patch.object(cli, "_stage_thumbnail", spy_thumb), \
                mock.patch.object(cli, "_stage_original", spy_original):
            cli.add_photo(self._photo_args())

        self.assertEqual(chamadas, [16, 16])
        self.assertEqual(len(destinos), 2)
        self.assertEqual(destinos[0].parent, self.thumbs)
        self.assertEqual(destinos[1].parent, self.photos)
        for destino in destinos:
            self.assertRegex(destino.name, TMP_NAME)

    def test_t4_sincronizacao_ocorre_antes_de_cada_replace(self):
        eventos = []
        real_fsync = os.fsync
        real_replace = os.replace

        def spy_fsync(fd):
            eventos.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
            return real_fsync(fd)

        def spy_replace(origem, destino):
            eventos.append(("replace", str(origem), str(destino)))
            return real_replace(origem, destino)

        with mock.patch.object(cli.os, "fsync", spy_fsync), \
                mock.patch.object(cli.os, "replace", spy_replace):
            cli.add_photo(self._photo_args())

        self.assertEqual([evento[0] for evento in eventos],
                         ["fsync", "replace", "fsync",
                          "fsync", "replace", "fsync"])

        thumb_tmp = eventos[0][1]
        self.assertEqual(Path(thumb_tmp).parent, self.thumbs)
        self.assertRegex(Path(thumb_tmp).name, TMP_NAME)
        self.assertEqual(eventos[1][1], thumb_tmp)
        self.assertEqual(Path(eventos[1][2]).parent, self.thumbs)
        self.assertEqual(eventos[2], ("fsync", str(self.thumbs)))

        original_tmp = eventos[3][1]
        self.assertEqual(Path(original_tmp).parent, self.photos)
        self.assertRegex(Path(original_tmp).name, TMP_NAME)
        self.assertEqual(eventos[4][1], original_tmp)
        self.assertEqual(Path(eventos[4][2]).parent, self.photos)
        self.assertEqual(eventos[5], ("fsync", str(self.photos)))

    def test_t5_falha_no_fsync_do_diretorio_da_miniatura_limpa_e_propaga(
        self
    ):
        with mock.patch.object(
            cli, "_fsync_dir",
            side_effect=OSError(errno.EIO, "fsync failed")
        ):
            with self.assertRaises(OSError) as contexto:
                cli.add_photo(self._photo_args())

        self.assertEqual(contexto.exception.errno, errno.EIO)
        self.assertEqual(self._names(self.photos), [])
        self.assertEqual(self._names(self.thumbs), [])
        self.assertEqual(self._photo_count(), 0)

    def test_t6_falha_no_fsync_do_diretorio_do_original_limpa_e_propaga(
        self
    ):
        with mock.patch.object(
            cli, "_fsync_dir",
            side_effect=[None, OSError(errno.EIO, "fsync failed")]
        ):
            with self.assertRaises(OSError) as contexto:
                cli.add_photo(self._photo_args())

        self.assertEqual(contexto.exception.errno, errno.EIO)
        self.assertEqual(self._names(self.photos), [])
        self.assertEqual(self._names(self.thumbs), [])
        self.assertEqual(self._photo_count(), 0)


    def test_t7_falha_na_insercao_do_banco_remove_arquivos_publicados(self):
        self._install_insert_trigger()

        with self.assertRaises(sqlite3.IntegrityError):
            cli.add_photo(self._photo_args())

        self.assertEqual(self._names(self.photos), [])
        self.assertEqual(self._names(self.thumbs), [])
        self.assertEqual(self._photo_count(), 0)

    def test_t8_keyboard_interrupt_durante_replace_limpa_temporarios(self):
        with mock.patch.object(
            cli.os, "replace", side_effect=KeyboardInterrupt("ctrl-c")
        ):
            with self.assertRaises(KeyboardInterrupt):
                cli.add_photo(self._photo_args())

        self.assertEqual(self._names(self.photos), [])
        self.assertEqual(self._names(self.thumbs), [])
        self.assertEqual(self._photo_count(), 0)

    def test_t9_keyboard_interrupt_apos_publicar_miniatura_limpa_publicado(
        self
    ):
        with mock.patch.object(
            cli, "_stage_original", side_effect=KeyboardInterrupt("ctrl-c")
        ):
            with self.assertRaises(KeyboardInterrupt):
                cli.add_photo(self._photo_args())

        self.assertEqual(self._names(self.photos), [])
        self.assertEqual(self._names(self.thumbs), [])
        self.assertEqual(self._photo_count(), 0)

    def test_t10_falha_na_limpeza_nao_mascara_excecao_original(self):
        self._install_insert_trigger()
        stderr = io.StringIO()

        with mock.patch.object(
            cli.Path, "unlink", side_effect=OSError(errno.EACCES, "denied")
        ), contextlib.redirect_stderr(stderr):
            with self.assertRaises(sqlite3.IntegrityError):
                cli.add_photo(self._photo_args())

        self.assertIn("Aviso: não foi possível remover", stderr.getvalue())

    def test_t11_imagem_invalida_preserva_causa_e_nao_deixa_arquivos(self):
        quebrada = self.base / "in" / "quebrada.jpg"
        quebrada.write_bytes(b"isto nao e uma imagem")

        with self.assertRaises(SystemExit) as contexto:
            cli.add_photo(self._photo_args(quebrada))

        self.assertIn("Imagem inválida", str(contexto.exception))
        self.assertIsNotNone(contexto.exception.__cause__)
        self.assertEqual(self._names(self.photos), [])
        self.assertEqual(self._names(self.thumbs), [])
        self.assertEqual(self._photo_count(), 0)

    def test_t12_fsync_dir_fecha_fd_mesmo_com_erro(self):
        with mock.patch.object(
            cli.os, "open", return_value=42
        ) as abertura, mock.patch.object(
            cli.os, "fsync", side_effect=OSError(errno.EIO, "boom")
        ) as sincronizacao, mock.patch.object(
            cli.os, "close"
        ) as fechamento:
            with self.assertRaises(OSError):
                cli._fsync_dir(self.base)

        abertura.assert_called_once()
        sincronizacao.assert_called_once_with(42)
        fechamento.assert_called_once_with(42)


    def test_t13_mensagem_inexistente_nao_cria_arquivos(self):
        args = types.SimpleNamespace(
            message="MSG-INEXISTENTE", file=str(self.source)
        )

        with self.assertRaises(SystemExit) as contexto:
            cli.add_photo(args)

        self.assertIn("Mensagem não encontrada", str(contexto.exception))
        self.assertEqual(self._names(self.photos), [])
        self.assertEqual(self._names(self.thumbs), [])
        self.assertEqual(self._photo_count(), 0)

    def test_t14_stage_original_com_modo_sem_leitura_do_dono(self):
        destino = self.photos / f".original.jpg.{'0' * 32}.tmp"
        real_copystat = shutil.copystat

        def copystat_trancando(source, target, **kwargs):
            real_copystat(source, target, **kwargs)
            os.chmod(target, 0o044)

        with mock.patch.object(cli.shutil, "copystat", copystat_trancando):
            cli._stage_original(self.source, destino)

        self.assertEqual(stat.S_IMODE(destino.stat().st_mode), 0o044)
        self.assertEqual(destino.stat().st_size, self.source.stat().st_size)
        self.assertEqual(
            destino.stat().st_mtime_ns, self.source.stat().st_mtime_ns
        )
        with self.assertRaises(PermissionError):
            os.open(destino, os.O_RDONLY)

    def test_t15_interrupcao_apos_replace_concluido_nao_deixa_orfao(self):
        real_replace = os.replace

        for posicao in (1, 2):
            with self.subTest(posicao=posicao):
                self._reset_all()
                chamadas = {"n": 0}

                def replace_e_interromper(origem, destino, _pos=posicao):
                    chamadas["n"] += 1
                    real_replace(origem, destino)
                    if chamadas["n"] == _pos:
                        raise KeyboardInterrupt(f"apos replace {_pos}")

                with mock.patch.object(
                    cli.os, "replace", replace_e_interromper
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        cli.add_photo(self._photo_args())

                self.assertEqual(self._names(self.photos), [])
                self.assertEqual(self._names(self.thumbs), [])
                self.assertEqual(self._photo_count(), 0)


    def test_t16_interrupcao_no_limite_do_commit(self):
        def commit_e_interromper(connection):
            connection.commit()
            raise KeyboardInterrupt("apos commit")

        with self.subTest(caso="commit confirmado"):
            with mock.patch.object(
                cli, "connect", self._armed_factory(2, commit_e_interromper)
            ):
                with self.assertRaises(KeyboardInterrupt):
                    cli.add_photo(self._photo_args())

            self.assertEqual(self._photo_count(), 1)
            self.assertEqual(len(self._names(self.photos)), 1)
            self.assertEqual(len(self._names(self.thumbs)), 1)
            self.assertEqual(self._temps(self.photos), [])
            self.assertEqual(self._temps(self.thumbs), [])
            self._reset_all()

        def commit_falhando(connection):
            raise sqlite3.OperationalError("commit failed")

        with self.subTest(caso="commit nao confirmado"):
            with mock.patch.object(
                cli, "connect", self._armed_factory(2, commit_falhando)
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    cli.add_photo(self._photo_args())

            self.assertEqual(self._photo_count(), 0)
            self.assertEqual(self._names(self.photos), [])
            self.assertEqual(self._names(self.thumbs), [])
            self._reset_all()

        with self.subTest(caso="confirmacao indisponivel"):
            self._install_insert_trigger()
            real_connect = cli.connect
            estado = {"n": 0}

            def fabrica_cega():
                estado["n"] += 1
                if estado["n"] >= 3:
                    raise sqlite3.OperationalError("probe unavailable")
                return real_connect()

            with mock.patch.object(cli, "connect", fabrica_cega):
                with self.assertRaises(sqlite3.IntegrityError):
                    cli.add_photo(self._photo_args())

            self.assertEqual(self._photo_count(), 0)
            self.assertEqual(len(self._names(self.photos)), 1)
            self.assertEqual(len(self._names(self.thumbs)), 1)
            self._drop_insert_trigger()
            self._reset_all()


    def test_t21_falha_antes_do_banco_remove_tudo_sem_consultar(self):
        real_replace = os.replace
        chamadas = {"n": 0}

        def replace_falhando_na_segunda(origem, destino):
            chamadas["n"] += 1
            if chamadas["n"] == 1:
                return real_replace(origem, destino)
            raise OSError(errno.EIO, "falha antes da etapa de banco")

        with mock.patch.object(
            cli.os, "replace", replace_falhando_na_segunda
        ), mock.patch.object(
            cli, "_photo_exists",
            side_effect=sqlite3.OperationalError("sqlite indisponivel")
        ) as consulta:
            with self.assertRaises(OSError) as contexto:
                cli.add_photo(self._photo_args())

        self.assertEqual(contexto.exception.errno, errno.EIO)
        self.assertEqual(
            str(contexto.exception), "[Errno 5] falha antes da etapa de banco"
        )
        consulta.assert_not_called()
        self.assertEqual(self._names(self.photos), [])
        self.assertEqual(self._names(self.thumbs), [])
        self.assertEqual(self._photo_count(), 0)

    def test_t22_miniatura_240_quadrada_paisagem(self):
        origem = self._write_image(
            "paisagem.jpg", (1200, 800), "RGB", (20, 40, 60)
        )

        cli.add_photo(self._photo_args(origem))

        with Image.open(self._photo_row()["thumbnail_path"]) as imagem:
            self.assertEqual(imagem.format, "JPEG")
            self.assertEqual(imagem.size, (240, 240))

    def test_t23_miniatura_240_quadrada_retrato(self):
        origem = self._write_image(
            "retrato.jpg", (800, 1200), "RGB", (60, 40, 20)
        )

        cli.add_photo(self._photo_args(origem))

        with Image.open(self._photo_row()["thumbnail_path"]) as imagem:
            self.assertEqual(imagem.size, (240, 240))

    def test_t24_miniatura_faz_corte_central(self):
        faixas = Image.new("RGB", (720, 240), (255, 0, 0))
        faixas.paste((0, 255, 0), (240, 0, 480, 240))
        faixas.paste((0, 0, 255), (480, 0, 720, 240))
        origem = self.base / "in" / "faixas.png"
        faixas.save(origem)

        cli.add_photo(self._photo_args(origem))

        with Image.open(self._photo_row()["thumbnail_path"]) as imagem:
            self.assertEqual(imagem.size, (240, 240))
            convertida = imagem.convert("RGB")
            for ponto in ((2, 2), (120, 120), (237, 237)):
                r, g, b = convertida.getpixel(ponto)
                self.assertGreater(g, 200)
                self.assertLess(r, 60)
                self.assertLess(b, 60)

    def test_t25_transparencia_composta_sobre_branco(self):
        transposta = Image.new("RGBA", (600, 600), (0, 0, 0, 0))
        transposta.paste((255, 0, 0, 255), (200, 200, 400, 400))
        origem = self.base / "in" / "transparente.png"
        transposta.save(origem)

        cli.add_photo(self._photo_args(origem))

        with Image.open(self._photo_row()["thumbnail_path"]) as imagem:
            convertida = imagem.convert("RGB")
            self.assertEqual(convertida.size, (240, 240))
            for canto in ((2, 2), (237, 2), (2, 237), (237, 237)):
                r, g, b = convertida.getpixel(canto)
                self.assertGreaterEqual(min(r, g, b), 245)
            r, g, b = convertida.getpixel((120, 120))
            self.assertGreater(r, 200)
            self.assertLess(g, 60)
            self.assertLess(b, 60)

    def test_t26_miniatura_amplia_imagem_pequena(self):
        origem = self._write_image(
            "icone.png", (100, 100), "RGB", (30, 120, 200)
        )

        cli.add_photo(self._photo_args(origem))

        with Image.open(self._photo_row()["thumbnail_path"]) as imagem:
            self.assertEqual(imagem.size, (240, 240))

    def test_t28_png_paleta_com_transparencia_por_indice(self):
        paleta = [0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254
        paletizada = Image.new("P", (600, 600), 0)
        paletizada.putpalette(paleta)
        paletizada.paste(1, (200, 200, 400, 400))
        paletizada.info["transparency"] = 0
        origem = self.base / "in" / "paleta.png"
        paletizada.save(origem, "PNG")

        with Image.open(origem) as conferida:
            self.assertEqual(conferida.mode, "P")
            self.assertEqual(conferida.info.get("transparency"), 0)

        cli.add_photo(self._photo_args(origem))

        with Image.open(self._photo_row()["thumbnail_path"]) as imagem:
            convertida = imagem.convert("RGB")
            self.assertEqual(convertida.size, (240, 240))
            for canto in ((2, 2), (237, 2), (2, 237), (237, 237)):
                r, g, b = convertida.getpixel(canto)
                self.assertGreaterEqual(min(r, g, b), 245)
            r, g, b = convertida.getpixel((120, 120))
            self.assertGreater(r, 200)
            self.assertLess(g, 60)
            self.assertLess(b, 60)


class ExportHtmlTest(CliTestCase):
    """T17-T20 e T27: publicacao atomica do HTML."""

    def test_t17_export_html_publica_conteudo_correto(self):
        self._add_photo_rows()
        destino = self.exports / f"{self.message_id}.html"
        destino.write_text("CONTEUDO ANTERIOR", encoding="utf-8")

        cli.export_html(self._export_args())
        conteudo = destino.read_text(encoding="utf-8")

        self.assertTrue(conteudo.startswith("<!DOCTYPE html>"))
        self.assertNotIn("CONTEUDO ANTERIOR", conteudo)
        self.assertIn("https://photos.example/t/THUMB1", conteudo)
        self.assertIn("https://photos.example/o/ORIG2", conteudo)
        self.assertIn("Fotos &amp; Cia", conteudo)
        self.assertIn("ORIG1.jpg", conteudo)
        self.assertIn('width="120" height="120"', conteudo)
        self.assertIn("object-fit:cover", conteudo)
        self.assertIn('loading="lazy"', conteudo)
        self.assertEqual(self._names(self.exports), [destino.name])
        self.assertEqual(self._temps(self.exports), [])

        cli.export_html(self._export_args())
        self.assertEqual(destino.read_text(encoding="utf-8"), conteudo)
        self.assertEqual(self._temps(self.exports), [])

    def test_t18_export_html_falha_na_gravacao_preserva_anterior(self):
        self._add_photo_rows()
        destino = self.exports / f"{self.message_id}.html"

        casos = (
            (
                "fsync do arquivo",
                lambda: mock.patch.object(
                    cli.os, "fsync",
                    side_effect=OSError(errno.ENOSPC, "No space left")
                ),
                OSError,
            ),
            (
                "replace",
                lambda: mock.patch.object(
                    cli.os, "replace",
                    side_effect=KeyboardInterrupt("ctrl-c")
                ),
                KeyboardInterrupt,
            ),
        )

        for rotulo, patcher, excecao in casos:
            with self.subTest(caso=rotulo):
                self._reset()
                destino.write_text("CONTEUDO ANTERIOR", encoding="utf-8")

                with patcher():
                    with self.assertRaises(excecao):
                        cli.export_html(self._export_args())

                self.assertEqual(
                    destino.read_text(encoding="utf-8"), "CONTEUDO ANTERIOR"
                )
                self.assertEqual(self._names(self.exports), [destino.name])
                self.assertEqual(self._temps(self.exports), [])


    def test_t19_export_html_falha_no_fsync_do_diretorio(self):
        self._add_photo_rows()
        destino = self.exports / f"{self.message_id}.html"
        destino.write_text("CONTEUDO ANTERIOR", encoding="utf-8")

        with mock.patch.object(
            cli, "_fsync_dir", side_effect=OSError(errno.EIO, "fsync failed")
        ):
            with self.assertRaises(OSError) as contexto:
                cli.export_html(self._export_args())

        self.assertEqual(contexto.exception.errno, errno.EIO)
        self.assertTrue(destino.is_file())
        conteudo = destino.read_text(encoding="utf-8")
        self.assertNotIn("CONTEUDO ANTERIOR", conteudo)
        self.assertTrue(conteudo.startswith("<!DOCTYPE html>"))
        self.assertEqual(self._temps(self.exports), [])

    def test_t20_export_html_sem_temporarios_apos_sucesso_ou_falha(self):
        self._add_photo_rows()
        destino = self.exports / f"{self.message_id}.html"

        with self.subTest(caso="sucesso"):
            self._reset()
            cli.export_html(self._export_args())
            self.assertEqual(self._temps(self.exports), [])

        with self.subTest(caso="falha de gravacao"):
            self._reset()
            destino.write_text("CONTEUDO ANTERIOR", encoding="utf-8")
            with mock.patch.object(
                cli.os, "fsync",
                side_effect=OSError(errno.EIO, "fsync failed")
            ):
                with self.assertRaises(OSError):
                    cli.export_html(self._export_args())
            self.assertEqual(self._temps(self.exports), [])

        with self.subTest(caso="falha de diretorio"):
            self._reset()
            with mock.patch.object(
                cli, "_fsync_dir",
                side_effect=OSError(errno.EIO, "fsync failed")
            ):
                with self.assertRaises(OSError):
                    cli.export_html(self._export_args())
            self.assertEqual(self._temps(self.exports), [])

        with self.subTest(caso="limpeza recebe apenas o temporario"):
            self._reset()
            destino.write_text("CONTEUDO ANTERIOR", encoding="utf-8")
            descartados = []

            with mock.patch.object(
                cli, "_discard",
                side_effect=lambda paths: descartados.append(list(paths))
            ), mock.patch.object(
                cli.os, "fsync",
                side_effect=OSError(errno.EIO, "fsync failed")
            ):
                with self.assertRaises(OSError):
                    cli.export_html(self._export_args())

            self.assertEqual(len(descartados), 1)
            self.assertEqual(len(descartados[0]), 1)
            self.assertNotIn(destino, descartados[0])
            self.assertRegex(descartados[0][0].name, TMP_NAME)
            self.assertEqual(
                destino.read_text(encoding="utf-8"), "CONTEUDO ANTERIOR"
            )

    def test_t27_html_compativel_com_miniatura_legada(self):
        legado = self.thumbs / "LEGADA.jpg"
        Image.new("RGB", (480, 360), (200, 10, 10)).save(legado, "JPEG")
        conteudo_antes = legado.read_bytes()
        mtime_antes = legado.stat().st_mtime_ns

        with self._connect() as db:
            db.execute(
                "INSERT INTO photos (message_id, original_token, "
                "thumbnail_token, filename, original_path, thumbnail_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.message_id,
                    "ORIGLEG",
                    "THUMBLEG",
                    "legada.jpg",
                    "/data/photos/ORIGLEG.jpg",
                    str(legado),
                )
            )

        cli.export_html(self._export_args())
        html = (self.exports / f"{self.message_id}.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('width="120" height="120"', html)
        self.assertIn("object-fit:cover", html)
        self.assertIn("https://photos.example/t/THUMBLEG", html)
        self.assertEqual(legado.read_bytes(), conteudo_antes)
        self.assertEqual(legado.stat().st_mtime_ns, mtime_antes)


class CliInterfaceTest(CliTestCase):
    """CT1-CT30: resolucao de mensagem, lote, base URL, limites, saidas,
    codigos de retorno e erros operacionais."""

    def test_ct1_add_photo_usa_ultima_mensagem(self):
        self._nova_mensagem("MSG-NOVA", "2030-01-01T00:00:00.000Z")

        cli.add_photo(types.SimpleNamespace(files=[str(self.source)]))

        with self._connect() as db:
            row = db.execute("SELECT message_id FROM photos").fetchone()
        self.assertEqual(row["message_id"], "MSG-NOVA")

    def test_ct2_export_html_usa_ultima_mensagem(self):
        self._nova_mensagem("MSG-NOVA", "2030-01-01T00:00:00.000Z")
        with self._connect() as db:
            db.execute(
                "INSERT INTO photos (message_id, original_token, "
                "thumbnail_token, filename, original_path, thumbnail_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("MSG-NOVA", "ONOVA", "TNOVA", "n.jpg",
                 "/data/photos/ONOVA.jpg", "/data/thumbnails/TNOVA.jpg")
            )

        cli.export_html(types.SimpleNamespace(
            message=None, base_url="https://photos.example", output=None
        ))

        self.assertTrue((self.exports / "MSG-NOVA.html").is_file())

    def test_ct3_add_photo_mensagem_explicita_por_id(self):
        self._nova_mensagem("MSG-NOVA", "2030-01-01T00:00:00.000Z")

        cli.add_photo(types.SimpleNamespace(
            message=self.message_id, files=[str(self.source)]
        ))

        with self._connect() as db:
            row = db.execute("SELECT message_id FROM photos").fetchone()
        self.assertEqual(row["message_id"], self.message_id)

    def test_ct4_mensagem_inexistente_orienta_list(self):
        with self.assertRaises(SystemExit) as contexto:
            cli.add_photo(types.SimpleNamespace(
                message="MSG-INEXISTENTE", files=[str(self.source)]
            ))

        self.assertEqual(contexto.exception.code, 1)
        self.assertIn("Mensagem não encontrada", str(contexto.exception))
        self.assertIn("mailtrack list", str(contexto.exception))
        self.assertEqual(self._photo_count(), 0)
        self.assertEqual(self._names(self.photos), [])

    def test_ct5_sem_mensagens_orienta_create(self):
        with self._connect() as db:
            db.execute("DELETE FROM messages")

        with self.assertRaises(SystemExit) as contexto:
            cli.add_photo(types.SimpleNamespace(files=[str(self.source)]))

        self.assertEqual(contexto.exception.code, 1)
        self.assertIn("Nenhuma mensagem cadastrada", str(contexto.exception))
        self.assertIn("mailtrack create", str(contexto.exception))

    def test_ct6_add_photo_em_lote(self):
        fontes = [
            self._write_image(f"lote{i}.jpg", (300, 300))
            for i in (1, 2, 3)
        ]
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida):
            cli.add_photo(types.SimpleNamespace(
                files=[str(fonte) for fonte in fontes]
            ))

        self.assertEqual(self._photo_count(), 3)
        self.assertEqual(len(self._names(self.photos)), 3)
        self.assertEqual(len(self._names(self.thumbs)), 3)
        self.assertEqual(self._temps(self.photos), [])
        self.assertIn("Resumo: 3 adicionada(s), 0 falha(s)", saida.getvalue())

    def test_ct7_add_photo_falha_parcial_informa_e_nao_desfaz(self):
        primeira = self._write_image("ok1.jpg", (300, 300))
        quebrada = self.base / "in" / "ruim.jpg"
        quebrada.write_bytes(b"isto nao e uma imagem")
        segunda = self._write_image("ok2.jpg", (300, 300))
        saida, erros = io.StringIO(), io.StringIO()

        with contextlib.redirect_stdout(saida), \
                contextlib.redirect_stderr(erros):
            with self.assertRaises(SystemExit) as contexto:
                cli.add_photo(types.SimpleNamespace(
                    files=[str(primeira), str(quebrada), str(segunda)]
                ))

        self.assertEqual(contexto.exception.code, 1)
        self.assertEqual(self._photo_count(), 2)
        self.assertEqual(len(self._names(self.photos)), 2)
        self.assertIn(
            "Resumo: 2 adicionada(s), 1 falha(s)", saida.getvalue()
        )
        self.assertIn("ruim.jpg", erros.getvalue())

    def test_ct8_add_photo_sintaxe_antiga(self):
        self._nova_mensagem("MSG-AB12CD34EF", "2029-01-01T00:00:00.000Z")

        cli.add_photo(types.SimpleNamespace(
            files=["MSG-AB12CD34EF", str(self.source)]
        ))

        self.assertEqual(self._photo_count(), 1)
        with self._connect() as db:
            row = db.execute("SELECT message_id FROM photos").fetchone()
        self.assertEqual(row["message_id"], "MSG-AB12CD34EF")

    def test_ct9_add_photo_informa_urls(self):
        saida = io.StringIO()
        with mock.patch.dict(
            os.environ, {"MAILTRACK_BASE_URL": cli.BASE_URL}
        ), contextlib.redirect_stdout(saida):
            cli.add_photo(types.SimpleNamespace(files=[str(self.source)]))

        texto = saida.getvalue()
        self.assertIn(f"{cli.BASE_URL}/t/", texto)
        self.assertIn(f"{cli.BASE_URL}/o/", texto)
        self.assertIn("OK      foto.jpg", texto)


    def test_ct10_base_url_precedencia(self):
        self._add_photo_rows()
        destino = self.exports / f"{self.message_id}.html"

        with mock.patch.dict(
            os.environ, {"MAILTRACK_BASE_URL": "https://env.example"}
        ):
            cli.export_html(types.SimpleNamespace(
                message=self.message_id, base_url=None, output=None
            ))
            self.assertIn(
                "https://env.example/t/THUMB1",
                destino.read_text(encoding="utf-8")
            )

            cli.export_html(types.SimpleNamespace(
                message=self.message_id,
                base_url="https://flag.example",
                output=None
            ))
            texto = destino.read_text(encoding="utf-8")
            self.assertIn("https://flag.example/t/THUMB1", texto)
            self.assertNotIn("https://env.example", texto)

        with mock.patch.dict(os.environ, {"MAILTRACK_BASE_URL": ""}):
            cli.export_html(types.SimpleNamespace(
                message=self.message_id, base_url=None, output=None
            ))
            self.assertIn(
                f"{cli.BASE_URL}/t/THUMB1",
                destino.read_text(encoding="utf-8")
            )

    def test_ct11_export_html_com_output(self):
        self._add_photo_rows()
        destino = self.base / "saida" / "manual.html"
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida):
            cli.export_html(types.SimpleNamespace(
                message=self.message_id,
                base_url="https://x.example",
                output=str(destino)
            ))

        self.assertTrue(destino.is_file())
        self.assertIn(
            "https://x.example/t/THUMB1",
            destino.read_text(encoding="utf-8")
        )
        self.assertEqual(self._temps(destino.parent), [])
        self.assertIn("HTML gerado:", saida.getvalue())
        self.assertIn(destino.name, saida.getvalue())

    def test_ct12_export_html_imprime_proximo_passo(self):
        cli.add_photo(self._photo_args())
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida):
            cli.export_html(self._export_args())

        texto = saida.getvalue()
        self.assertIn("HTML gerado:", texto)
        self.assertIn("Próximo passo:", texto)

    def test_ct13_list_marca_ultima_e_idade(self):
        self._nova_mensagem("MSG-NOVA", "2030-01-01T00:00:00.000Z")
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida):
            cli.list_messages(types.SimpleNamespace(limit=None, json=False))

        texto = saida.getvalue()
        linhas = [linha for linha in texto.splitlines() if "|" in linha]
        self.assertTrue(linhas[0].startswith("* MSG-NOVA"))
        self.assertTrue(linhas[1].startswith("  " + self.message_id))
        self.assertIn("agora", texto)
        self.assertIn("* = última mensagem", texto)

    def test_ct14_list_limit(self):
        self._nova_mensagem("MSG-N1", "2029-01-01T00:00:00.000Z")
        self._nova_mensagem("MSG-N2", "2029-06-01T00:00:00.000Z")
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida):
            cli.list_messages(types.SimpleNamespace(limit=2, json=False))

        linhas = [linha for linha in saida.getvalue().splitlines()
                  if "|" in linha]
        self.assertEqual(len(linhas), 2)
        self.assertIn("MSG-N2", linhas[0])
        self.assertIn("MSG-N1", linhas[1])
        self.assertTrue(linhas[0].startswith("* MSG-N2"))

    def test_ct15_list_json(self):
        self._nova_mensagem("MSG-NOVA", "2030-01-01T00:00:00.000Z")
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida):
            cli.list_messages(types.SimpleNamespace(limit=None, json=True))

        dados = json.loads(saida.getvalue())
        self.assertEqual(dados[0]["id"], "MSG-NOVA")
        self.assertTrue(dados[0]["is_last"])
        self.assertFalse(dados[1]["is_last"])
        self.assertEqual(dados[0]["photo_count"], 0)
        self.assertIn("age_seconds", dados[0])
        self.assertIn("created_at", dados[0])


    def test_ct16_show_mostra_links_e_tamanho(self):
        cli.add_photo(self._photo_args())
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida):
            cli.show_message(types.SimpleNamespace(
                message=self.message_id,
                base_url="https://photos.example",
                json=False
            ))

        texto = saida.getvalue()
        self.assertIn("foto.jpg", texto)
        self.assertIn("tamanho:", texto)
        self.assertIn("miniatura: https://photos.example/t/", texto)
        self.assertIn("original:  https://photos.example/o/", texto)

    def test_ct17_show_json(self):
        cli.add_photo(self._photo_args())
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida):
            cli.show_message(types.SimpleNamespace(
                message=None,
                base_url="https://photos.example",
                json=True
            ))

        dados = json.loads(saida.getvalue())
        self.assertEqual(dados["id"], self.message_id)
        self.assertEqual(len(dados["photos"]), 1)
        foto = dados["photos"][0]
        self.assertEqual(foto["filename"], "foto.jpg")
        self.assertGreater(foto["size_bytes"], 0)
        self.assertTrue(
            foto["thumbnail_url"].startswith("https://photos.example/t/")
        )
        self.assertTrue(
            foto["original_url"].startswith("https://photos.example/o/")
        )

    def test_ct18_history_limite_e_resumo(self):
        with self._connect() as db:
            for indice in range(1, 6):
                db.execute(
                    "INSERT INTO events (message_id, event_type, "
                    "occurred_at) VALUES (?, ?, ?)",
                    (self.message_id, f"evento{indice}",
                     f"2026-11-{10 + indice:02d}T00:00:00.000Z")
                )

        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            cli.history(types.SimpleNamespace(
                message=self.message_id, limit=2, summary=True
            ))

        texto = saida.getvalue()
        linhas = self._event_lines(texto)
        self.assertEqual(len(linhas), 2)
        # Os 2 mais recentes, exibidos em ordem cronológica.
        self.assertIn("evento4", linhas[0])
        self.assertIn("evento5", linhas[1])
        self.assertNotIn("evento1", texto)
        self.assertNotIn("IP visitante", texto)
        self.assertIn("MAILTRACK — HISTÓRICO DE ACESSOS", texto)

    def test_ct19_history_padrao_compacto(self):
        with self._connect() as db:
            db.execute(
                "INSERT INTO events (message_id, event_type) VALUES (?, ?)",
                (self.message_id, "thumbnail_requested")
            )

        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            cli.history(types.SimpleNamespace(message=self.message_id))

        texto = saida.getvalue()
        self.assertIn("MAILTRACK — HISTÓRICO DE ACESSOS", texto)
        self.assertIn(f"Mensagem: {self.message_id}", texto)
        self.assertIn(f"Assunto: {self.subject}", texto)
        self.assertIn("Miniatura", texto)
        self.assertNotIn("thumbnail_requested", texto)
        self.assertNotIn("IP visitante", texto)
        self.assertNotIn("User-Agent:", texto)


    def test_ct20_version(self):
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida), \
                self.assertRaises(SystemExit) as contexto:
            self._main("--version")

        self.assertEqual(contexto.exception.code, 0)
        self.assertIn(cli.VERSION, saida.getvalue())

    def test_ct21_uso_invalido_retorna_2(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as contexto:
            self._main("create")

        self.assertEqual(contexto.exception.code, 2)

    def test_ct22_erro_operacional_retorna_1(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as contexto:
            self._main("export-html", "MSG-INEXISTENTE")

        self.assertEqual(contexto.exception.code, 1)

    def test_ct23_main_add_photo_com_mensagem_e_lote(self):
        self._nova_mensagem("MSG-AB12CD34EF", "2029-01-01T00:00:00.000Z")
        segunda = self._write_image("outra.jpg", (300, 300))

        with contextlib.redirect_stdout(io.StringIO()):
            self._main(
                "add-photo", "--message", "MSG-AB12CD34EF",
                str(self.source), str(segunda)
            )

        self.assertEqual(self._photo_count(), 2)
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM photos WHERE message_id=?",
                ("MSG-AB12CD34EF",)
            ).fetchone()
        self.assertEqual(row["n"], 2)

    def test_ct24_main_fluxo_padrao(self):
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida):
            self._main(
                "create", "--to", "dest@example.com", "--subject", "Fotos"
            )
            self._main("add-photo", str(self.source))
            self._main("export-html")

        texto = saida.getvalue()
        self.assertRegex(texto, r"Mensagem criada: MSG-[0-9A-F]{10}")
        self.assertIn("HTML gerado:", texto)

        gerados = sorted(self.exports.glob("*.html"))
        self.assertEqual(len(gerados), 1)
        self.assertIn("foto.jpg", gerados[0].read_text(encoding="utf-8"))


    def test_ct25_list_limit_zero_invalido(self):
        erros = io.StringIO()

        with contextlib.redirect_stderr(erros), \
                self.assertRaises(SystemExit) as contexto:
            self._main("list", "--limit", "0")

        self.assertEqual(contexto.exception.code, 2)
        self.assertIn("limite", erros.getvalue())

    def test_ct26_list_limit_negativo_invalido(self):
        erros = io.StringIO()

        with contextlib.redirect_stderr(erros), \
                self.assertRaises(SystemExit) as contexto:
            self._main("list", "--limit", "-1")

        self.assertEqual(contexto.exception.code, 2)
        self.assertIn("maior que zero", erros.getvalue())
        self.assertIn("-1", erros.getvalue())

    def test_ct27_history_limit_negativo_invalido(self):
        erros = io.StringIO()

        with contextlib.redirect_stderr(erros), \
                self.assertRaises(SystemExit) as contexto:
            self._main("history", "--limit", "-1")

        self.assertEqual(contexto.exception.code, 2)
        self.assertIn("não pode ser negativo", erros.getvalue())
        self.assertIn("use 0 para todos os eventos", erros.getvalue())

    def test_ct28_history_limit_zero_mostra_todos(self):
        with self._connect() as db:
            for indice in (1, 2, 3):
                db.execute(
                    "INSERT INTO events (message_id, event_type) "
                    "VALUES (?, ?)",
                    (self.message_id, f"evento{indice}")
                )

        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            self._main("history", "--limit", "0", "--summary")

        linhas = self._event_lines(saida.getvalue())
        self.assertEqual(len(linhas), 3)
        self.assertIn("evento1", saida.getvalue())
        self.assertIn("evento3", saida.getvalue())

    def test_ct29_erro_de_io_vira_mensagem_e_codigo_1(self):
        self._add_photo_rows()
        erros = io.StringIO()

        with mock.patch.object(
            cli,
            "_stage_html",
            side_effect=OSError(errno.ENOSPC, "No space left on device")
        ), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(erros), \
                self.assertRaises(SystemExit) as contexto:
            self._main("export-html")

        self.assertEqual(contexto.exception.code, 1)
        texto = erros.getvalue()
        self.assertIn("Erro operacional (OSError)", texto)
        self.assertIn("No space left on device", texto)
        self.assertNotIn("Traceback", texto)

    def test_ct30_erro_de_banco_vira_mensagem_e_codigo_1(self):
        erros = io.StringIO()

        with mock.patch.object(
            cli,
            "connect",
            side_effect=sqlite3.OperationalError("database is locked")
        ), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(erros), \
                self.assertRaises(SystemExit) as contexto:
            self._main("list")

        self.assertEqual(contexto.exception.code, 1)
        texto = erros.getvalue()
        self.assertIn("Erro operacional (OperationalError)", texto)
        self.assertIn("database is locked", texto)
        self.assertNotIn("Traceback", texto)


class HistoryTest(CliTestCase):
    """H1-H14: formato compacto do histórico de acessos."""

    def _history(self, *argv):
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            self._main("history", *argv)
        return saida.getvalue()

    @staticmethod
    def _restaurar_tz(anterior):
        if anterior is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = anterior
        time.tzset()

    def test_h1_multiplos_acessos_formato_compacto(self):
        foto1 = self._insert_photo("foto1.jpg", "ORIG1", "THUMB1")
        foto2 = self._insert_photo("foto2.jpg", "ORIG2", "THUMB2")
        chrome_windows = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self._insert_event(
            "original_requested", "2026-11-16T21:42:15.000Z",
            photo_id=foto1,
            remote_ip="200.100.10.20",
            client_ip="172.19.0.1",
            user_agent=chrome_windows,
        )
        self._insert_event(
            "thumbnail_requested", "2026-11-16T21:42:18.000Z",
            photo_id=foto1,
            remote_ip="200.100.10.20",
            client_ip="172.19.0.1",
            user_agent=chrome_windows,
        )
        self._insert_event(
            "original_requested", "2026-11-16T21:45:03.000Z",
            photo_id=foto2,
            remote_ip="189.20.30.40",
        )

        texto = self._history(self.message_id)

        self.assertIn("MAILTRACK — HISTÓRICO DE ACESSOS", texto)
        self.assertIn(f"Mensagem: {self.message_id}", texto)
        self.assertIn(f"Assunto: {self.subject}", texto)

        linhas = self._event_lines(texto)
        self.assertEqual(len(linhas), 3)
        partes = [parte.strip() for parte in linhas[0].split(" | ")]
        self.assertRegex(
            partes[0], r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$"
        )
        self.assertEqual(
            partes[1:],
            [
                "foto1.jpg",
                "Original",
                "IP 200.100.10.20",
                "Chrome / Windows",
            ],
        )
        self.assertIn("| foto1.jpg | Miniatura |", linhas[1])
        self.assertIn("| foto2.jpg | Original |", linhas[2])
        self.assertNotIn("172.19.0.1", texto)
        self.assertNotIn("Mozilla/5.0", texto)

    def test_h2_identifica_miniatura_e_original(self):
        foto = self._insert_photo("foto.jpg", "ORIG-H2", "THUMB-H2")
        self._insert_event(
            "thumbnail_requested", "2026-11-16T21:00:00.000Z",
            photo_id=foto,
        )
        self._insert_event(
            "original_requested", "2026-11-16T21:00:01.000Z",
            photo_id=foto,
        )

        texto = self._history(self.message_id)
        linhas = self._event_lines(texto)

        self.assertIn("| Miniatura", linhas[0])
        self.assertIn("| Original", linhas[1])
        self.assertNotIn("thumbnail_requested", texto)
        self.assertNotIn("original_requested", texto)

    def test_h3_nome_original_do_arquivo(self):
        foto = self._insert_photo(
            "Fotos do sítio.jpg", "ORIG-H3", "THUMB-H3"
        )
        self._insert_event(
            "original_requested", "2026-11-16T21:00:00.000Z",
            photo_id=foto,
        )

        texto = self._history(self.message_id)
        self.assertIn("| Fotos do sítio.jpg | Original", texto)

    def test_h4_ip_disponivel_e_ausente(self):
        foto = self._insert_photo("foto.jpg", "ORIG-H4", "THUMB-H4")
        self._insert_event(
            "original_requested", "2026-11-16T21:00:00.000Z",
            photo_id=foto,
            remote_ip="203.0.113.9",
            client_ip="172.19.0.1",
        )
        self._insert_event(
            "original_requested", "2026-11-16T21:00:01.000Z",
            photo_id=foto,
            client_ip="10.0.0.5",
        )
        self._insert_event(
            "original_requested", "2026-11-16T21:00:02.000Z",
            photo_id=foto,
        )

        texto = self._history(self.message_id)
        linhas = self._event_lines(texto)

        self.assertIn("IP 203.0.113.9", linhas[0])
        self.assertNotIn("172.19.0.1", linhas[0])
        self.assertIn("IP 10.0.0.5", linhas[1])
        self.assertNotIn("IP ", linhas[2])
        self.assertNotIn("IP -", texto)

    def test_h5_user_agent_disponivel_ausente_e_resumido(self):
        foto = self._insert_photo("foto.jpg", "ORIG-H5", "THUMB-H5")
        agentes = (
            (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/157.0.0.0 Mobile Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko "
                "Firefox/11.0 (via ggpht.com GoogleImageProxy)"
            ),
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/42.0.2311.135 Safari/537.36 Edge/12.246"
            ),
            None,
        )
        for indice, agente in enumerate(agentes):
            self._insert_event(
                "original_requested",
                f"2026-11-16T21:00:0{indice}.000Z",
                photo_id=foto,
                user_agent=agente,
            )

        texto = self._history(self.message_id)
        linhas = self._event_lines(texto)

        self.assertIn("Chrome / Linux", linhas[0])
        self.assertIn("Chrome / Android", linhas[1])
        self.assertIn("Firefox / Windows", linhas[2])
        self.assertIn("Edge / Windows", linhas[3])
        self.assertEqual(len(linhas[4].split(" | ")), 3)
        self.assertNotIn("Mozilla/5.0", texto)

    def test_h6_eventos_desconhecidos_sao_preservados(self):
        self._insert_event(
            "message_created", "2026-11-16T21:00:00.000Z"
        )

        texto = self._history(self.message_id)
        linhas = self._event_lines(texto)

        self.assertEqual(len(linhas), 1)
        self.assertIn("| message_created", linhas[0])
        self.assertNotIn(" | -", linhas[0])

    def test_h7_ordem_cronologica(self):
        for nome, quando in (
            ("fotoC.jpg", "2026-11-16T21:03:00.000Z"),
            ("fotoA.jpg", "2026-11-16T21:01:00.000Z"),
            ("fotoB.jpg", "2026-11-16T21:02:00.000Z"),
        ):
            foto = self._insert_photo(
                nome, f"ORIG-{nome}", f"THUMB-{nome}"
            )
            self._insert_event(
                "original_requested", quando, photo_id=foto
            )

        # Empate no horário: o id desempata na ordem crescente.
        foto_b2 = self._insert_photo("fotoB2.jpg", "ORIG-B2", "THUMB-B2")
        self._insert_event(
            "original_requested", "2026-11-16T21:02:00.000Z",
            photo_id=foto_b2,
        )

        linhas = self._event_lines(self._history(self.message_id))
        nomes = [linha.split(" | ")[1].strip() for linha in linhas]
        self.assertEqual(
            nomes,
            ["fotoA.jpg", "fotoB.jpg", "fotoB2.jpg", "fotoC.jpg"],
        )

    def test_h8_limit_e_limit_zero(self):
        for indice in range(1, 6):
            self._insert_event(
                f"evento{indice}",
                f"2026-11-{10 + indice:02d}T00:00:00.000Z",
            )

        limitados = self._event_lines(self._history("--limit", "2"))
        self.assertEqual(len(limitados), 2)
        self.assertIn("evento4", limitados[0])
        self.assertIn("evento5", limitados[1])

        todos = self._event_lines(self._history("--limit", "0"))
        self.assertEqual(len(todos), 5)
        self.assertIn("evento1", todos[0])
        self.assertIn("evento5", todos[-1])

    def test_h9_historico_sem_id_usa_ultima_mensagem(self):
        self._insert_event(
            "original_requested", "2026-11-16T21:00:00.000Z"
        )
        self._nova_mensagem(
            "MSG-NOVA", "2030-01-01T00:00:00.000Z", subject="Assunto novo"
        )
        foto = self._insert_photo(
            "nova.jpg", "ORIG-NOVA", "THUMB-NOVA", message_id="MSG-NOVA"
        )
        self._insert_event(
            "thumbnail_requested", "2026-11-16T22:00:00.000Z",
            photo_id=foto,
            message_id="MSG-NOVA",
        )

        texto = self._history()

        self.assertIn("Mensagem: MSG-NOVA", texto)
        self.assertIn("Assunto: Assunto novo", texto)
        self.assertIn("nova.jpg", texto)
        self.assertNotIn(self.message_id, texto)

    def test_h10_summary_compativel(self):
        foto = self._insert_photo("foto.jpg", "ORIG-H10", "THUMB-H10")
        self._insert_event(
            "thumbnail_requested", "2026-11-16T21:00:00.000Z",
            photo_id=foto,
            remote_ip="203.0.113.10",
        )
        self._insert_event(
            "original_requested", "2026-11-16T21:00:01.000Z",
            photo_id=foto,
            remote_ip="203.0.113.10",
        )

        sem_resumo = self._history("--limit", "0")
        com_resumo = self._history("--limit", "0", "--summary")
        self.assertEqual(com_resumo, sem_resumo)

    def test_h11_data_hora_no_fuso_local(self):
        anterior = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self._restaurar_tz, anterior)

        self._insert_event(
            "original_requested", "2026-11-16T21:42:15.000Z"
        )

        self.assertIn("16/11/2026 21:42:15", self._history(self.message_id))

    def test_h12_sem_eventos(self):
        texto = self._history(self.message_id)

        self.assertIn("Nenhum evento registrado.", texto)
        self.assertNotIn("MAILTRACK", texto)

    def test_h13_campos_vazios_nao_aparecem(self):
        self._insert_event(
            "message_created", "2026-11-16T21:42:15.000Z"
        )

        texto = self._history(self.message_id)
        linha = self._event_lines(texto)[0]

        self.assertEqual(len(linha.split(" | ")), 2)
        self.assertNotIn(" | -", linha)
        self.assertNotIn("IP -", texto)
        self.assertNotIn("User-Agent", texto)

    def test_h14_limit_padrao_20(self):
        for indice in range(1, 23):
            self._insert_event(
                f"evento{indice}",
                f"2026-11-16T21:{indice:02d}:00.000Z",
            )

        texto = self._history(self.message_id)
        linhas = self._event_lines(texto)

        self.assertEqual(len(linhas), 20)
        self.assertIn("evento3", linhas[0])
        self.assertIn("evento22", linhas[-1])
        tipos = [linha.split(" | ")[-1] for linha in linhas]
        self.assertNotIn("evento1", tipos)
        self.assertNotIn("evento2", tipos)


class NewCommandTest(CliTestCase):
    """N1-N17: assistente `mailtrack new` e ajuda curta."""

    def _run_new(self, respostas):
        saida, erros = io.StringIO(), io.StringIO()
        codigo = 0
        restantes = list(respostas)

        def fake_input(prompt=""):
            print(prompt, end="")
            valor = restantes.pop(0)
            if isinstance(valor, BaseException):
                raise valor
            return valor

        with mock.patch("builtins.input", side_effect=fake_input), \
                contextlib.redirect_stdout(saida), \
                contextlib.redirect_stderr(erros):
            try:
                self._main("new")
            except SystemExit as exc:
                codigo = exc.code
        return saida.getvalue(), erros.getvalue(), codigo

    def _messages_for(self, email):
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM messages WHERE recipient=? ORDER BY rowid",
                (email,),
            ).fetchall()

    def _photos_for(self, message_id):
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM photos WHERE message_id=? ORDER BY id",
                (message_id,),
            ).fetchall()

    def test_n1_ajuda_curta_sem_argumentos(self):
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida):
            self._main()

        texto = saida.getvalue()
        self.assertIn(
            "MAILTRACK — fotografias com histórico de acessos", texto
        )
        self.assertIn("mailtrack new", texto)
        self.assertIn("assistente interativo (recomendado)", texto)
        self.assertIn("Início rápido:", texto)
        self.assertIn("mailtrack --help", texto)
        self.assertNotIn("usage:", texto)

    def test_n2_ajuda_completa_lista_todos_os_comandos(self):
        saida = io.StringIO()

        with contextlib.redirect_stdout(saida), \
                self.assertRaises(SystemExit) as contexto:
            self._main("--help")

        self.assertEqual(contexto.exception.code, 0)
        texto = saida.getvalue()
        for comando in (
            "new", "create", "add-photo", "export-html",
            "list", "show", "history",
        ):
            self.assertIn(comando, texto)

    def test_n3_fluxo_completo_com_arquivo_com_espacos(self):
        segunda = self._write_image("foto com espaço.jpg", (300, 300))
        texto, erros, codigo = self._run_new([
            "nova@example.com",
            "Fotos do sítio",
            str(self.source),
            str(segunda),
            "",
            "S",
        ])

        self.assertEqual(codigo, 0)
        self.assertIn("Resumo:", texto)
        self.assertIn("Destinatário: nova@example.com", texto)
        self.assertIn("Assunto: Fotos do sítio", texto)
        self.assertIn("Fotografias: 2", texto)
        self.assertIn("1. foto.jpg", texto)
        self.assertIn("2. foto com espaço.jpg", texto)
        self.assertIn("Mensagem criada: MSG-", texto)
        self.assertIn("HTML gerado:", texto)
        self.assertIn("Mensagem pronta para uso:", texto)
        self.assertIn("Gmail", texto)
        self.assertIn("Outlook", texto)
        self.assertIn("não envia o e-mail", texto)
        self.assertNotIn("FALHOU", texto)
        self.assertNotIn("FALHOU", erros)

        mensagens = self._messages_for("nova@example.com")
        self.assertEqual(len(mensagens), 1)
        message_id = mensagens[0]["id"]
        self.assertEqual(mensagens[0]["subject"], "Fotos do sítio")

        fotos = self._photos_for(message_id)
        self.assertEqual(
            [foto["filename"] for foto in fotos],
            ["foto.jpg", "foto com espaço.jpg"],
        )

        destino = self.exports / f"{message_id}.html"
        self.assertTrue(destino.is_file())
        html = destino.read_text(encoding="utf-8")
        self.assertIn("Fotos do sítio", html)
        self.assertIn("foto.jpg", html)
        self.assertIn("foto com espaço.jpg", html)
        self.assertIn(str(destino.resolve()), texto)

        self.assertLess(
            texto.index("Destinatário: "), texto.index("Assunto: ")
        )
        self.assertLess(
            texto.index("Assunto: "),
            texto.index("Fotografia (Enter para encerrar): "),
        )
        self.assertLess(
            texto.index("Fotografia (Enter para encerrar): "),
            texto.index("Confirma a criação"),
        )

    def test_n4_recusa_com_enter_nao_cria_nada(self):
        texto, _erros, codigo = self._run_new([
            "nova@example.com",
            "Fotos",
            str(self.source),
            "",
            "",
        ])

        self.assertEqual(codigo, 0)
        self.assertIn("Resumo:", texto)
        self.assertIn(
            "Operação cancelada. Nenhuma mensagem foi criada.", texto
        )
        self.assertEqual(self._messages_for("nova@example.com"), [])
        self.assertEqual(self._photo_count(), 0)
        self.assertEqual(list(self.exports.glob("*.html")), [])

    def test_n5_email_invalido_repete_ate_valido(self):
        texto, _erros, codigo = self._run_new([
            "",
            "invalido",
            "a@b@c",
            "a@b, c@d",
            "ok@example.com",
            "Assunto",
            str(self.source),
            "",
            "n",
        ])

        self.assertEqual(codigo, 0)
        self.assertIn("Informe um destinatário.", texto)
        self.assertIn(
            "E-mail inválido. Informe um único endereço de e-mail.", texto
        )
        self.assertIn("Operação cancelada.", texto)
        self.assertEqual(self._messages_for("ok@example.com"), [])
        self.assertEqual(self._photo_count(), 0)

    def test_n6_assunto_vazio_e_permitido(self):
        texto, _erros, codigo = self._run_new([
            "nova@example.com",
            "",
            str(self.source),
            "",
            "s",
        ])

        self.assertEqual(codigo, 0)
        self.assertIn("(sem assunto)", texto)

        mensagens = self._messages_for("nova@example.com")
        self.assertEqual(len(mensagens), 1)
        message_id = mensagens[0]["id"]
        self.assertEqual(mensagens[0]["subject"], "")

        destino = self.exports / f"{message_id}.html"
        self.assertIn(
            "<title>Fotografias</title>",
            destino.read_text(encoding="utf-8"),
        )

    def test_n7_caminho_inexistente_permite_corrigir(self):
        ausente = self.base / "in" / "não existe.jpg"
        texto, _erros, codigo = self._run_new([
            "nova@example.com",
            "Fotos",
            str(ausente),
            str(self.source),
            "",
            "s",
        ])

        self.assertEqual(codigo, 0)
        self.assertIn(f"Arquivo não encontrado: {ausente}", texto)
        self.assertIn("Fotografias: 1", texto)
        mensagens = self._messages_for("nova@example.com")
        self.assertEqual(len(mensagens), 1)
        self.assertEqual(len(self._photos_for(mensagens[0]["id"])), 1)

    def test_n8_imagem_invalida_e_extensao_nao_permitida(self):
        quebrada = self.base / "in" / "quebrada.jpg"
        quebrada.write_bytes(b"isto nao e uma imagem")
        texto_plano = self.base / "in" / "foto.txt"
        texto_plano.write_text("nao e imagem", encoding="utf-8")

        texto, _erros, codigo = self._run_new([
            "nova@example.com",
            "Fotos",
            str(quebrada),
            str(texto_plano),
            str(self.source),
            "",
            "s",
        ])

        self.assertEqual(codigo, 0)
        self.assertIn("Imagem inválida ou não suportada:", texto)
        self.assertIn("Formato de imagem não permitido: .txt", texto)
        self.assertIn("Fotografias: 1", texto)
        mensagens = self._messages_for("nova@example.com")
        self.assertEqual(len(mensagens), 1)
        self.assertEqual(len(self._photos_for(mensagens[0]["id"])), 1)

    def test_n9_nenhuma_foto_aborta_sem_criar(self):
        texto, erros, codigo = self._run_new([
            "nova@example.com",
            "Fotos",
            "",
        ])

        self.assertEqual(codigo, 1)
        self.assertIn("Nenhuma fotografia selecionada.", erros)
        self.assertIn("Nenhuma mensagem foi criada.", erros)
        self.assertNotIn("Resumo:", texto)
        self.assertEqual(self._messages_for("nova@example.com"), [])
        self.assertEqual(list(self.exports.glob("*.html")), [])

    def test_n10_ctrl_c_no_inicio_cancela_sem_criar(self):
        _texto, erros, codigo = self._run_new([KeyboardInterrupt()])

        self.assertEqual(codigo, 130)
        self.assertIn(
            "Operação cancelada. Nenhuma mensagem foi criada.", erros
        )
        self.assertEqual(self._messages_for("nova@example.com"), [])
        self.assertEqual(self._photo_count(), 0)
        self.assertEqual(list(self.exports.glob("*.html")), [])

    def test_n11_eof_cancela_sem_criar(self):
        _texto, erros, codigo = self._run_new([EOFError()])

        self.assertEqual(codigo, 1)
        self.assertIn("Entrada encerrada; operação cancelada.", erros)
        self.assertIn("Nenhuma mensagem foi criada.", erros)
        self.assertEqual(self._messages_for("nova@example.com"), [])
        self.assertEqual(self._photo_count(), 0)

    def test_n12_falha_parcial_preserva_mensagem_sem_html(self):
        segunda = self._write_image("falha.jpg", (300, 300))
        real_add_one = cli._add_one

        def parcial(source, message_id):
            if source.name == "falha.jpg":
                raise cli._Abort("falha simulada")
            return real_add_one(source, message_id)

        with mock.patch.object(cli, "_add_one", side_effect=parcial):
            _texto, erros, codigo = self._run_new([
                "nova@example.com",
                "Fotos",
                str(self.source),
                str(segunda),
                "",
                "s",
            ])

        self.assertEqual(codigo, 1)
        mensagens = self._messages_for("nova@example.com")
        self.assertEqual(len(mensagens), 1)
        message_id = mensagens[0]["id"]
        self.assertEqual(len(self._photos_for(message_id)), 1)
        self.assertIn("falha.jpg", erros)
        self.assertIn("1 de 2 fotografia(s)", erros)
        self.assertIn("HTML não foi gerado", erros)
        self.assertIn(f"mailtrack export-html {message_id}", erros)
        self.assertEqual(list(self.exports.glob("*.html")), [])

    def test_n13_zero_fotos_descarta_mensagem_vazia(self):
        segunda = self._write_image("falha.jpg", (300, 300))

        with mock.patch.object(
            cli, "_add_one", side_effect=cli._Abort("falha simulada")
        ):
            _texto, erros, codigo = self._run_new([
                "nova@example.com",
                "Fotos",
                str(self.source),
                str(segunda),
                "",
                "s",
            ])

        self.assertEqual(codigo, 1)
        self.assertEqual(self._messages_for("nova@example.com"), [])
        self.assertEqual(self._photo_count(), 0)
        self.assertIn("Nenhuma fotografia foi adicionada.", erros)
        self.assertIn("recém-criada foi descartada", erros)
        self.assertIn("HTML não foi gerado", erros)
        self.assertEqual(list(self.exports.glob("*.html")), [])

    def test_n14_falha_no_html_preserva_mensagem_e_fotos(self):
        with mock.patch.object(
            cli,
            "_stage_html",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            texto, erros, codigo = self._run_new([
                "nova@example.com",
                "Fotos",
                str(self.source),
                "",
                "s",
            ])

        self.assertEqual(codigo, 1)
        mensagens = self._messages_for("nova@example.com")
        self.assertEqual(len(mensagens), 1)
        message_id = mensagens[0]["id"]
        self.assertEqual(len(self._photos_for(message_id)), 1)
        self.assertIn("Mensagem criada: MSG-", texto)
        self.assertNotIn("Mensagem pronta para uso:", texto)
        self.assertIn("HTML não foi gerado", erros)
        self.assertIn("No space left on device", erros)
        self.assertIn(f"mailtrack export-html {message_id}", erros)
        self.assertEqual(list(self.exports.glob("*.html")), [])
        self.assertEqual(self._temps(self.exports), [])

    def test_n15_ctrl_c_durante_inclusao_descarta_mensagem_vazia(self):
        with mock.patch.object(
            cli.os, "replace", side_effect=KeyboardInterrupt("ctrl-c")
        ):
            texto, erros, codigo = self._run_new([
                "nova@example.com",
                "Fotos",
                str(self.source),
                "",
                "s",
            ])

        self.assertEqual(codigo, 130)
        self.assertEqual(self._messages_for("nova@example.com"), [])
        self.assertEqual(self._photo_count(), 0)
        self.assertEqual(self._names(self.photos), [])
        self.assertEqual(self._names(self.thumbs), [])
        self.assertIn("Mensagem criada: MSG-", texto)
        self.assertNotIn("Mensagem pronta para uso:", texto)
        self.assertIn("Operação cancelada.", erros)
        self.assertIn("descartada porque nenhuma fotografia", erros)
        self.assertEqual(list(self.exports.glob("*.html")), [])

    def test_n16_descarte_so_sem_fotos_e_eventos(self):
        self._nova_mensagem("MSG-VAZIA", "2029-01-01T00:00:00.000Z")
        self.assertTrue(cli._discard_empty_message("MSG-VAZIA"))

        self._nova_mensagem("MSG-EVENTO", "2029-01-01T00:00:00.000Z")
        self._insert_event(
            "message_created", "2029-01-01T00:00:00.000Z",
            message_id="MSG-EVENTO",
        )
        self.assertFalse(cli._discard_empty_message("MSG-EVENTO"))

        self._nova_mensagem("MSG-FOTO", "2029-01-01T00:00:00.000Z")
        self._insert_photo(
            "foto.jpg", "ORIG-GUARD", "THUMB-GUARD",
            message_id="MSG-FOTO",
        )
        self.assertFalse(cli._discard_empty_message("MSG-FOTO"))

        with self._connect() as db:
            existentes = db.execute(
                "SELECT id FROM messages WHERE id IN "
                "('MSG-EVENTO', 'MSG-FOTO') ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [row["id"] for row in existentes], ["MSG-EVENTO", "MSG-FOTO"]
        )

    def test_n17_ctrl_c_apos_primeira_foto_preserva_mensagem(self):
        segunda = self._write_image("segunda.jpg", (300, 300))
        real_replace = cli.os.replace
        chamadas = {"n": 0}

        def replace_interrupt(origem, destino):
            chamadas["n"] += 1
            if chamadas["n"] == 3:
                raise KeyboardInterrupt("ctrl-c")
            return real_replace(origem, destino)

        with mock.patch.object(
            cli.os, "replace", side_effect=replace_interrupt
        ):
            texto, erros, codigo = self._run_new([
                "nova@example.com",
                "Fotos",
                str(self.source),
                str(segunda),
                "",
                "s",
            ])

        self.assertEqual(codigo, 130)
        mensagens = self._messages_for("nova@example.com")
        self.assertEqual(len(mensagens), 1)
        message_id = mensagens[0]["id"]

        fotos = self._photos_for(message_id)
        self.assertEqual(len(fotos), 1)
        self.assertEqual(fotos[0]["filename"], "foto.jpg")
        self.assertTrue(Path(fotos[0]["original_path"]).is_file())
        self.assertTrue(Path(fotos[0]["thumbnail_path"]).is_file())
        self.assertEqual(len(self._names(self.photos)), 1)
        self.assertEqual(len(self._names(self.thumbs)), 1)
        self.assertEqual(self._temps(self.photos), [])
        self.assertEqual(self._temps(self.thumbs), [])

        self.assertIn("Mensagem criada: MSG-", texto)
        self.assertNotIn("Mensagem pronta para uso:", texto)
        self.assertIn("pode estar incompleta", erros)
        self.assertIn(message_id, erros)
        self.assertIn(
            f"mailtrack add-photo {message_id} FOTO...", erros
        )
        self.assertIn(f"mailtrack export-html {message_id}", erros)
        self.assertEqual(list(self.exports.glob("*.html")), [])


if __name__ == "__main__":
    unittest.main()
