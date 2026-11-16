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
        linhas = [linha for linha in texto.splitlines() if linha]
        self.assertEqual(len(linhas), 2)
        # Os 2 mais recentes, exibidos em ordem cronológica.
        self.assertIn("evento4", linhas[0])
        self.assertIn("evento5", linhas[1])
        self.assertNotIn("evento1", texto)
        self.assertNotIn("IP visitante", texto)

    def test_ct19_history_padrao_detalhado(self):
        with self._connect() as db:
            db.execute(
                "INSERT INTO events (message_id, event_type) VALUES (?, ?)",
                (self.message_id, "thumbnail_requested")
            )

        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            cli.history(types.SimpleNamespace(message=self.message_id))

        texto = saida.getvalue()
        self.assertIn("thumbnail_requested", texto)
        self.assertIn("IP visitante (Cloudflare):", texto)
        self.assertIn("User-Agent:", texto)


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

        linhas = [linha for linha in saida.getvalue().splitlines() if linha]
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


if __name__ == "__main__":
    unittest.main()
