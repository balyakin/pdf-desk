"""Границы приложения. SDK подставляется только вместо обращения к модели."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from PyPDF2 import PdfReader, PdfWriter

from make_demo_pdf import make_demo_pdf
import reader
import storage


class PdfTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.pdf_path = Path(self.temporary.name) / "пример.pdf"
        make_demo_pdf(self.pdf_path)
        self.data = self.pdf_path.read_bytes()

    def test_demo_and_physical_second_page(self):
        self.assertEqual(reader.inspect_pdf(self.data, "пример.PDF"),
                         {"page_count": 3, "empty_text_pages": []})
        image, text = reader.read_page(self.pdf_path, 2)
        self.addCleanup(image.close)
        self.assertIn("18 months", text)
        self.assertNotIn("Cover page", text)
        self.assertLessEqual(max(image.size), 1600)
        self.assertEqual(len(image.getpixel((0, 0))), 3)

    def test_invalid_page_numbers(self):
        for page in [0, True, 4, "2", 2.0, -1]:
            with self.subTest(page=page), self.assertRaises(reader.AppError):
                reader.read_page(self.pdf_path, page)

    def test_invalid_format_and_byte_limit(self):
        for data, name in [(b"", "a.pdf"), (b"not a PDF", "a.pdf"),
                           (self.data, "a.txt")]:
            with self.subTest(name=name, size=len(data)):
                with self.assertRaisesRegex(reader.AppError, "открыть PDF"):
                    reader.inspect_pdf(data, name)
        with self.assertRaisesRegex(reader.AppError, "25 МиБ"):
            reader.inspect_pdf(b"x" * (25 * 1024 * 1024 + 1), "a.pdf")

    def test_password_blank_and_page_limit(self):
        writer = PdfWriter()
        for page in PdfReader(io.BytesIO(self.data)).pages:
            writer.add_page(page)
        writer.encrypt("demo-password")
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(reader.AppError, "паролем"):
            reader.inspect_pdf(output.getvalue(), "a.pdf")
        for count, message in [(0, ""), (1, "недостаточно"),
                               (501, "500")]:
            writer = PdfWriter()
            for _ in range(count):
                writer.add_blank_page(width=900, height=400)
            output = io.BytesIO()
            writer.write(output)
            with self.subTest(count=count), self.assertRaisesRegex(reader.AppError, message):
                reader.inspect_pdf(output.getvalue(), "a.pdf")

    def test_partial_text_and_minimum_text(self):
        writer = PdfWriter()
        writer.add_page(PdfReader(io.BytesIO(self.data)).pages[1])
        writer.add_blank_page(width=900, height=400)
        output = io.BytesIO()
        writer.write(output)
        self.assertEqual(reader.inspect_pdf(output.getvalue(), "a.pdf"),
                         {"page_count": 2, "empty_text_pages": [2]})
        short = self.data.replace(b"Alpha-7 equipment manual. Cover page.", b" " * 36)
        short = short.replace(b"Warranty period: 18 months. Store below 45 C.", b" " * 43)
        short = short.replace(
            b"Warranty is void if the case is opened. Support: Mon-Fri 09:00-18:00.",
            b"abc" + b" " * 62,
        )
        with self.assertRaisesRegex(reader.AppError, "недостаточно"):
            reader.inspect_pdf(short, "a.pdf")


class UrlTests(unittest.TestCase):
    def test_normalizes_valid_addresses(self):
        for value, expected in [
            (" http://localhost:8000/v1/ ", "http://localhost:8000/v1"),
            ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1"),
            ("http://[::1]:8000/v1", "http://[::1]:8000/v1"),
            ("https://api.example.org/gateway/v1/", "https://api.example.org/gateway/v1"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(reader.validate_base_url(value), expected)

    def test_rejects_unsafe_addresses(self):
        for value in [
            "", "ftp://localhost/v1", "https://host/v2", "https://host:/v1",
            "https://user:secret@host/v1", "http://host/v1",
            "http://localhost.example.org/v1", "http://127.0.0.1.example.org/v1",
            "https://host/v1?key=secret", "https://host/v1#secret",
            "https://host\\other/v1", "https://host/space here/v1",
            "https://host/v1\n", "https://host:\t/v1", "https://host:0/v1",
            "https://host:65536/v1", "http://[::2]/v1", "https://[::1/v1",
        ]:
            with self.subTest(value=value), self.assertRaises(reader.AppError):
                reader.validate_base_url(value)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "data"
        sample = Path(self.temporary.name) / "sample.pdf"
        make_demo_pdf(sample)
        self.data = sample.read_bytes()
        self.profile = {"base_url": "http://127.0.0.1:8000/v1",
                        "index_model": "index-demo", "sdk_version": "0.2.20"}
        self.item_id = storage.make_item_id(self.data, **self.profile)
        folder = storage.item_dir(self.root, self.item_id)
        folder.mkdir(parents=True)
        (folder / "source.pdf").write_bytes(self.data)
        self.manifest = {"version": 1, "item_id": self.item_id,
                         "display_name": "Инструкция.pdf", "sdk_doc_id": "pi-demo",
                         "page_count": 3, "index_profile": self.profile}

    def test_hash_binds_pdf_and_index_profile(self):
        self.assertRegex(self.item_id, r"^[0-9a-f]{64}$")
        self.assertEqual(storage.make_item_id(self.data, **self.profile), self.item_id)
        for key, value in [("base_url", "http://localhost:8000/v1"),
                           ("index_model", "other"), ("sdk_version", "0.2.21")]:
            with self.subTest(key=key):
                self.assertNotEqual(storage.make_item_id(self.data, **{**self.profile, key: value}),
                                    self.item_id)
        self.assertNotEqual(storage.make_item_id(self.data + b"\n", **self.profile), self.item_id)

    def test_roundtrip_and_missing_manifest(self):
        self.assertIsNone(storage.load_active(self.root))
        storage.atomic_write_json(self.root / "active.json", self.manifest)
        self.assertEqual(storage.load_active(self.root), self.manifest)

    def test_bad_manifest_is_kept(self):
        path = self.root / "active.json"
        variants = [[], {**self.manifest, "version": True},
                    {**self.manifest, "version": 2}, {**self.manifest, "page_count": True},
                    {**self.manifest, "page_count": 501}, {**self.manifest, "page_count": 0},
                    {**self.manifest, "item_id": "../outside"},
                    {**self.manifest, "sdk_doc_id": None}, {**self.manifest, "display_name": []},
                    {**self.manifest, "index_profile": {**self.profile, "index_model": "other"}}]
        for value in variants:
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaisesRegex(reader.AppError, "выберите PDF заново"):
                    storage.load_active(self.root)
                self.assertEqual(path.read_bytes(), before)
        path.write_bytes(b"{broken")
        with self.assertRaises(reader.AppError):
            storage.load_active(self.root)
        storage.atomic_write_json(path, self.manifest)
        (storage.item_dir(self.root, self.item_id) / "source.pdf").unlink()
        with self.assertRaises(reader.AppError):
            storage.load_active(self.root)

    def test_failed_atomic_write_preserves_old_json(self):
        path = self.root / "active.json"
        storage.atomic_write_json(path, self.manifest)
        before = path.read_bytes()
        for failure in ["replace", "fsync"]:
            with self.subTest(failure=failure):
                with patch(f"storage.os.{failure}", side_effect=OSError("disk failure")):
                    with self.assertRaises(OSError):
                        storage.atomic_write_json(path, {"version": 2})
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(self.root.glob("*.tmp")), [])
        with self.assertRaises(ValueError):
            storage.atomic_write_json(path, {"number": float("nan")})
        self.assertEqual(path.read_bytes(), before)

    def test_rejects_path_traversal_and_symlinks(self):
        for value in ["../../outside", "A" * 64, "a" * 63, "a" * 65, None]:
            with self.subTest(value=value), self.assertRaises(reader.AppError):
                storage.item_dir(self.root, value)
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        linked = self.root / "items" / ("f" * 64)
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Система не разрешает символические ссылки: {error.errno}")
        with self.assertRaises(reader.AppError):
            storage.item_dir(self.root, "f" * 64)
        (self.root / "settings.json").symlink_to(outside / "settings.json")
        with self.assertRaises(reader.AppError):
            storage.atomic_write_json(self.root / "settings.json", {})
        self.assertEqual(list(outside.iterdir()), [])


class AnswerTests(unittest.TestCase):
    def test_rejects_invented_documents_invalid_pages_and_duplicates(self):
        citations = [{"doc_id": "pi-demo", "page": 2}] * 2 + [
            {"doc_id": doc, "page": page} for doc, page in
            [(None, 1), ("pi-other", 1), ("pi-demo", 0), ("pi-demo", 4),
             ("pi-demo", True), ("pi-demo", "2"), ("pi-demo", 2.0)]
        ] + [None, "page 2"]
        sources, rejected = reader.validate_citations(citations, "pi-demo", 3)
        self.assertEqual(sources, [{"doc_id": "pi-demo", "page": 2}])
        self.assertEqual(rejected, 9)
        with self.assertRaises(reader.AppError):
            reader.validate_citations({}, "pi-demo", 3)

    def test_questions_are_independent_and_validated_before_client(self):
        client = MagicMock()
        for question in ["", " \n ", "x" * 2001]:
            with self.subTest(question_length=len(question)), self.assertRaises(reader.AppError):
                reader.ask_pdf(client, "pi-demo", 3, question)
        client.chat.assert_not_called()
        client.chat.return_value = '18 months. <cite doc="source.pdf" page="2"/>'
        client.get_citations.return_value = [{"document": "source.pdf", "doc_id": "pi-demo", "page": 2}]
        answer = reader.ask_pdf(client, "pi-demo", 3, "  Срок гарантии?  ")
        self.assertEqual(answer, {"text": "18 months.",
                                 "sources": [{"doc_id": "pi-demo", "page": 2}], "warnings": []})
        client.chat.assert_called_once_with("Срок гарантии?", doc_id="pi-demo", citations=True,
                                            stream=False, show_process=False, max_turns=8)

    def test_missing_rejected_and_malformed_sources(self):
        client = MagicMock()
        client.chat.return_value = "Сведений недостаточно."
        for citations, warning_count in [([], 1), ([{"doc_id": None, "page": 1}], 2)]:
            client.get_citations.return_value = citations
            answer = reader.ask_pdf(client, "pi-demo", 3, "Цена?")
            self.assertEqual(answer["sources"], [])
            self.assertEqual(len(answer["warnings"]), warning_count)
            self.assertIn("без проверяемого источника", answer["warnings"][-1])
        client.get_citations.return_value = None
        with self.assertRaises(reader.AppError):
            reader.ask_pdf(client, "pi-demo", 3, "Цена?")

    def test_invalid_tags_dropped_by_sdk_still_warn(self):
        client = MagicMock()
        client.get_citations.return_value = [{"document": "source.pdf", "doc_id": "pi-demo", "page": 2}]
        for tag in ['<cite doc="source.pdf" page="0"/>', '<doc=source.pdf;page=4>',
                    '<cite doc="source.pdf" page="True"/>', '<cite doc="source.pdf"/>']:
            client.chat.return_value = 'Ответ<cite doc="source.pdf" page="2"/>' + tag
            with self.subTest(tag=tag):
                answer = reader.ask_pdf(client, "pi-demo", 3, "Вопрос?")
                self.assertEqual(answer["sources"], [{"doc_id": "pi-demo", "page": 2}])
                self.assertTrue(any("Часть ссылок" in warning for warning in answer["warnings"]))

    def test_empty_unknown_and_tag_only_answers(self):
        client = MagicMock()
        client.get_citations.return_value = []
        for raw in ["", " \n", None, {"answer": "not the SDK contract"}, "<cite doc=x page=1/>"]:
            client.chat.return_value = raw
            with self.subTest(raw=raw), self.assertRaises(reader.AppError):
                reader.ask_pdf(client, "pi-demo", 3, "Вопрос?")

    def test_citation_cleaning_keeps_wrapped_text_and_other_html(self):
        raw = '<CITE doc=x page=2>Полезный текст</CITE> <cite doc=x page=3/> <doc=x;page=2>'
        self.assertEqual(reader.clean_answer(raw), "Полезный текст")
        dangerous = '<script>alert(1)</script> ![image](https://example.org/track)'
        self.assertEqual(reader.clean_answer(dangerous), dangerous)

    def test_citation_cleaning_restores_punctuation_spacing(self):
        # ARRANGE
        raw = 'Срок гарантии — 18 месяцев <cite doc="source.pdf" page="2"/> .'

        # ACT
        answer = reader.clean_answer(raw)

        # ASSERT
        self.assertEqual(answer, 'Срок гарантии — 18 месяцев.')

    def test_citation_cleaning_handles_unicode_space_and_sentence_marks(self):
        # ARRANGE
        raw = 'Цена неизвестна\u00a0<cite doc="source.pdf" page="2"/>! Данные\u00a0<doc=source.pdf;page=2>: проверены.'

        # ACT
        answer = reader.clean_answer(raw)

        # ASSERT
        self.assertEqual(answer, 'Цена неизвестна! Данные: проверены.')

    def test_index_checks_sdk_shapes_and_page_count(self):
        client = MagicMock()
        client.submit_document.return_value = {"doc_id": "pi-demo", "name": "source.pdf"}
        client.get_document.return_value = {"id": "pi-demo", "name": "source.pdf",
                                            "status": "completed", "pageNum": 3}
        self.assertEqual(reader.index_pdf(client, Path("source.pdf"), 3), "pi-demo")
        for metadata in [None, [], {"status": "processing", "pageNum": 3},
                         {"status": "completed", "pageNum": True},
                         {"status": "completed", "pageNum": 2}]:
            client.get_document.return_value = metadata
            with self.subTest(metadata=metadata), self.assertRaises(reader.AppError):
                reader.index_pdf(client, Path("source.pdf"), 3)
        for submitted in [None, [], {}, {"doc_id": True}, {"doc_id": ""}]:
            client.submit_document.return_value = submitted
            with self.subTest(submitted=submitted), self.assertRaises(reader.AppError):
                reader.index_pdf(client, Path("source.pdf"), 3)


class ConnectionTests(unittest.TestCase):
    def response(self, payload):
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def test_probe_uses_exact_models_and_ping_without_upload(self):
        opener = MagicMock()
        opener.open.side_effect = [
            self.response({"choices": [{"message": {"content": "OK"}}]}),
            self.response({"choices": [{"message": {"tool_calls": [
                {"type": "function", "function": {"name": "ping", "arguments": "{}"}}
            ]}}]}),
        ]
        with patch("reader.build_opener", return_value=opener):
            reader.check_connection("http://localhost:8000/v1", "", "model-A", "model-A")
        self.assertEqual(opener.open.call_count, 2)
        first, second = opener.open.call_args_list
        self.assertEqual(first.args[0].full_url, "http://localhost:8000/v1/chat/completions")
        self.assertEqual(first.kwargs["timeout"], 15)
        self.assertEqual(first.args[0].get_header("Authorization"), "Bearer local")
        self.assertEqual(json.loads(first.args[0].data)["model"], "model-A")
        tool_probe = json.loads(second.args[0].data)
        self.assertEqual(tool_probe["model"], "model-A")
        self.assertEqual(tool_probe["tools"][0]["function"]["name"], "ping")
        self.assertEqual(tool_probe["tool_choice"], "auto")
        self.assertFalse(tool_probe["stream"])

    def test_rejects_missing_tool_wrong_arguments_and_unknown_function(self):
        for tool_calls in [[], None, [{"function": {"name": "execute", "arguments": "{}"}}],
                           [{"function": {"name": "ping", "arguments": "[]"}}],
                           [{"function": {"name": "ping", "arguments": "broken"}}],
                           [{"function": {"name": "ping", "arguments": '{"x":1}'}}]]:
            opener = MagicMock()
            opener.open.side_effect = [
                self.response({"choices": [{"message": {"content": "OK"}}]}),
                self.response({"choices": [{"message": {"tool_calls": tool_calls}}]}),
            ]
            with self.subTest(tool_calls=tool_calls), patch("reader.build_opener", return_value=opener):
                with self.assertRaisesRegex(reader.AppError, "вызова инструментов"):
                    reader.check_connection("https://api.example.org/v1", "secret", "index", "chat")

    def test_redirects_and_http_errors_never_expose_key(self):
        for code, message in [(302, "перенаправление"), (401, "ключ"), (429, "ограничил"),
                              (404, "совместимую модель"), (503, "не ответил")]:
            opener = MagicMock()
            opener.open.side_effect = HTTPError("https://host/v1", code, "secret", {},
                                                io.BytesIO(b"secret error body"))
            with self.subTest(code=code), patch("reader.build_opener", return_value=opener):
                with self.assertRaises(reader.AppError) as raised:
                    reader.check_connection("https://host/v1", "secret", "index", "chat")
                self.assertIn(message, str(raised.exception))
                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(opener.open.call_count, 1)
        handler = reader._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://other/v1"))

    def test_remote_key_and_model_names_required_before_network(self):
        with patch("reader.build_opener") as opener:
            for key, index, chat in [("", "index", "chat"), ("secret", "", "chat"),
                                     ("secret", "index", " ")]:
                with self.subTest(index=index, chat=chat), self.assertRaises(reader.AppError):
                    reader.check_connection("https://host/v1", key, index, chat)
            opener.assert_not_called()

    def test_real_sdk_client_routes_to_one_server_without_global_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("OPENAI_API_KEY")
            client = reader.make_client("http://localhost:8000/v1", "session-secret",
                                        "vendor/index", "vendor/chat", Path(temporary).resolve() / "index")
            self.assertEqual(os.environ.get("OPENAI_API_KEY"), previous)
            self.assertEqual(client.index_model, "openai/vendor/index")
            self.assertEqual(client.chat_model, "openai/vendor/chat")
            self.assertEqual(client.chat_backend["base_url"], "http://localhost:8000/v1")
            self.assertEqual(client.chat_backend["timeout"], 60.0)
            self.assertEqual(client._api._index_backend["api_key"], "session-secret")
            self.assertEqual(Path(client.storage_path), Path(temporary).resolve() / "index")
            self.assertEqual(client.get_citations('<cite doc="invented.pdf" page="2"/>')[0]["doc_id"], None)


class ActionTests(unittest.TestCase):
    def setUp(self):
        import app
        self.app = app
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "data"
        path = Path(self.temporary.name) / "sample.pdf"
        make_demo_pdf(path)
        self.data = path.read_bytes()
        self.settings = {"base_url": "http://127.0.0.1:8000/v1", "api_key": "session-secret",
                         "index_model": "index-demo", "chat_model": "chat-demo"}
        self.client = MagicMock()
        self.client.submit_document.return_value = {"doc_id": "pi-demo", "name": "source.pdf"}
        self.client.get_document.return_value = {"id": "pi-demo", "name": "source.pdf",
                                                "status": "completed", "pageNum": 3}
        self.client_patch = patch("reader.make_client", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def prepare(self, data=None, name="manual.pdf", settings=None):
        return self.app.prepare_document(self.root, self.data if data is None else data,
                                          name, self.settings if settings is None else settings)

    def test_failed_preparation_and_commit_keep_previous_document(self):
        old, _ = self.prepare()
        manifest_bytes = (self.root / "active.json").read_bytes()
        pdf_bytes = (storage.item_dir(self.root, old["item_id"]) / "source.pdf").read_bytes()
        for failure in ["index", "commit"]:
            with self.subTest(failure=failure):
                target = "reader.index_pdf" if failure == "index" else "storage.os.replace"
                with patch(target, side_effect=OSError("secret server body")):
                    with self.assertRaises(reader.AppError) as raised:
                        self.prepare(self.data + b"\n% replacement\n")
                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual((self.root / "active.json").read_bytes(), manifest_bytes)
                self.assertEqual((storage.item_dir(self.root, old["item_id"]) / "source.pdf").read_bytes(), pdf_bytes)
                self.assertEqual(list((self.root / "items").iterdir()), [storage.item_dir(self.root, old["item_id"])])

    def test_completed_commit_cannot_be_undone_by_temp_cleanup(self):
        self.prepare()
        original_unlink = Path.unlink

        def fail_temp_cleanup(path, *args, **kwargs):
            if path.suffix == ".tmp":
                raise PermissionError("temporary cleanup failed")
            return original_unlink(path, *args, **kwargs)

        data = self.data + b"\n% replacement\n"
        with patch.object(Path, "unlink", fail_temp_cleanup):
            active, _ = self.prepare(data=data)
        self.assertEqual(storage.load_active(self.root), active)
        self.assertEqual((storage.item_dir(self.root, active["item_id"]) / "source.pdf").read_bytes(), data)

    def test_same_pdf_and_chat_change_reuse_completed_index(self):
        first, _ = self.prepare()
        second, _ = self.prepare(settings={**self.settings, "chat_model": "other-chat"})
        self.assertEqual(first["item_id"], second["item_id"])
        self.client.submit_document.assert_called_once()
        self.client.get_document.return_value = {"status": "failed", "pageNum": 3}
        before = (self.root / "active.json").read_bytes()
        with self.assertRaises(reader.AppError):
            self.prepare()
        self.assertEqual((self.root / "active.json").read_bytes(), before)
        self.client.submit_document.assert_called_once()

    def test_corrupt_manifest_or_missing_pdf_recovers_completed_sdk_index(self):
        first, _ = self.prepare()
        folder = storage.item_dir(self.root, first["item_id"])
        (folder / "index").mkdir()
        self.client.list_documents.return_value = {
            "documents": [{"id": "pi-demo", "name": "source.pdf", "status": "completed",
                           "pageNum": 3, "description": None, "createdAt": "2026-10-01T00:00:00",
                           "folderId": None, "path": None, "metadata": None, "features": {}}],
            "total": 1, "limit": 2, "offset": 0,
        }
        for failure in ["manifest", "PDF"]:
            with self.subTest(failure=failure):
                if failure == "manifest":
                    (self.root / "active.json").write_bytes(b"{broken")
                else:
                    (folder / "source.pdf").unlink()
                restored, _ = self.prepare()
                self.assertEqual(restored, first)
                self.assertEqual(storage.load_active(self.root), first)
                self.client.submit_document.assert_called_once()

    def test_unsafe_display_name_never_becomes_disk_path(self):
        manifest, _ = self.prepare(name="../../outside.pdf")
        self.assertEqual(manifest["display_name"], "../../outside.pdf")
        self.assertEqual((storage.item_dir(self.root, manifest["item_id"]) / "source.pdf").read_bytes(), self.data)
        self.assertFalse((Path(self.temporary.name) / "outside.pdf").exists())

    def test_invalid_pdf_never_reaches_sdk(self):
        for data, name in [(b"", "a.pdf"), (b"broken", "a.pdf"), (self.data, "a.txt"),
                           (b"x" * (25 * 1024 * 1024 + 1), "a.pdf")]:
            with self.subTest(size=len(data), name=name), self.assertRaises(reader.AppError):
                self.prepare(data=data, name=name)
        self.client.submit_document.assert_not_called()
        self.assertFalse((self.root / "active.json").exists())

    def test_busy_operation_and_error_release_the_lock(self):
        with self.app.operation():
            with self.assertRaisesRegex(reader.AppError, "Уже выполняется операция"):
                self.prepare()
        self.client.submit_document.assert_not_called()
        with self.assertRaises(RuntimeError):
            with self.app.operation():
                raise RuntimeError("controlled failure")
        self.assertTrue(storage.LOCK.acquire(blocking=False))
        storage.LOCK.release()

    def test_document_and_settings_changes_clear_result(self):
        manifest, _ = self.prepare()
        state = {"active_item_id": "previous", "view_page": 3,
                 "result": {"item_id": "previous", "text": "old answer"}}
        self.app.sync_state(state, manifest, self.settings)
        self.assertIsNone(state["result"])
        self.assertEqual(state["view_page"], 1)
        state["result"] = {"item_id": manifest["item_id"], "text": "answer"}
        state["checked_settings"] = self.app.settings_fingerprint(self.settings)
        self.app.sync_state(state, manifest, {**self.settings, "api_key": "new-secret"})
        self.assertIsNone(state["checked_settings"])
        self.assertIsNone(state["result"])

    def test_source_callback_does_not_chat(self):
        state = {"view_page": 1}
        with patch.object(self.app.st, "session_state", state):
            self.app.select_page(2)
        self.assertEqual(state["view_page"], 2)
        self.client.chat.assert_not_called()

    def test_question_requires_current_document_and_matching_profile(self):
        manifest, _ = self.prepare()
        for item_id, settings in [("a" * 64, self.settings),
                                  (manifest["item_id"], {**self.settings, "index_model": "other"})]:
            with self.subTest(item_id=item_id), self.assertRaises(reader.AppError):
                self.app.answer_question(self.root, item_id, settings, "Вопрос?")
        self.client.chat.assert_not_called()
        self.client.chat.return_value = "Нет сведений."
        self.client.get_citations.return_value = []
        answer = self.app.answer_question(self.root, manifest["item_id"], self.settings, "Цена?")
        self.assertEqual(answer["item_id"], manifest["item_id"])
        self.assertEqual(answer["question"], "Цена?")

    def test_settings_persist_without_key_or_checked_flag(self):
        with patch("reader.check_connection"):
            self.app.verify_connection(self.root, self.settings)
        saved = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"base_url", "index_model", "chat_model", "is_local"})
        self.assertNotIn("session-secret", json.dumps(saved))
        self.assertTrue(saved["is_local"])

    def test_import_does_not_start_actions(self):
        import importlib
        with patch("reader.check_connection") as check:
            importlib.reload(self.app)
        check.assert_not_called()
        self.client.submit_document.assert_not_called()


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        import app
        self.app = app
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "data"
        path = Path(self.temporary.name) / "demo.pdf"
        make_demo_pdf(path)
        self.data = path.read_bytes()
        self.script = f"from pathlib import Path\nimport app\napp.DATA_ROOT = Path({str(self.root)!r})\napp.main()\n"
        self.client = MagicMock()
        self.client.submit_document.return_value = {"doc_id": "pi-demo", "name": "source.pdf"}
        self.client.get_document.return_value = {"id": "pi-demo", "name": "source.pdf",
                                                "status": "completed", "pageNum": 3}
        self.client.chat.return_value = 'Проверяемый ответ <script>alert(1)</script><cite doc="source.pdf" page="2"/>'
        self.client.get_citations.return_value = [
            {"document": "source.pdf", "doc_id": "pi-demo", "page": 2},
            {"document": "source.pdf", "doc_id": "pi-demo", "page": 2},
            {"document": "source.pdf", "doc_id": "pi-demo", "page": 3},
        ]
        client_patch = patch("reader.make_client", return_value=self.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.at = AppTest.from_string(self.script, default_timeout=20).run()

    def upload(self, data=None):
        from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec
        from streamlit.proto.Common_pb2 import FileURLs
        self.at.session_state["uploaded_pdf"] = UploadedFile(
            UploadedFileRec("demo", "demo.pdf", "application/pdf", self.data if data is None else data),
            FileURLs(),
        )
        self.at.run()

    def prepare(self):
        self.at.text_input(key="index_model").set_value("index-demo")
        self.at.text_input(key="chat_model").set_value("chat-demo")
        with patch("reader.check_connection"):
            self.at.button(key="check_connection").click().run()
        self.upload()
        self.client.submit_document.assert_not_called()
        self.at.button(key="prepare_document").click().run()
        self.assertEqual(len(self.at.exception), 0)

    def ask(self, question="Срок гарантии?"):
        self.at.text_area(key="question").set_value(question)
        next(button for button in self.at.button if button.label == "Получить ответ").click().run()
        self.assertEqual(len(self.at.exception), 0)

    def test_upload_reruns_and_source_navigation_do_not_call_model(self):
        self.assertTrue(self.at.button(key="prepare_document").disabled)
        self.prepare()
        self.ask()
        result = self.at.session_state["result"]
        self.assertIn('<script>alert(1)</script>', next(text.value for text in self.at.text
                                                     if text.value.startswith("Проверяемый ответ")))
        self.assertEqual(len(result["sources"]), 2)
        self.at.button(key=f"source-{result['item_id']}-2").click().run()
        self.assertEqual(self.at.session_state["view_page"], 2)
        self.assertTrue(any("18 months" in text.value for text in self.at.text))
        self.at.number_input(key="view_page").set_value(3).run()
        self.assertTrue(next(button for button in self.at.button if button.label == "Следующая").disabled)
        self.at.run()
        self.client.chat.assert_called_once()
        self.client.submit_document.assert_called_once()

    def test_changes_invalidate_check_and_answer(self):
        self.prepare()
        self.ask()
        self.at.text_input(key="api_key").set_value("new-session-secret").run()
        self.assertIsNone(self.at.session_state["checked_settings"])
        self.assertIsNone(self.at.session_state["result"])
        self.assertTrue(self.at.button(key="prepare_document").disabled)
        self.assertFalse(any(text.value.startswith("Проверяемый ответ") for text in self.at.text))

    def test_actions_reject_settings_changed_in_same_event(self):
        self.at.text_input(key="index_model").set_value("index-demo")
        self.at.text_input(key="chat_model").set_value("chat-demo")
        with patch("reader.check_connection"):
            self.at.button(key="check_connection").click().run()
        self.upload()
        self.assertFalse(self.at.button(key="prepare_document").disabled)
        self.at.text_input(key="api_key").set_value("changed-key")
        self.at.button(key="prepare_document").click().run()
        self.client.submit_document.assert_not_called()
        self.assertEqual(len(self.at.exception), 0)

        self.prepare()
        button = next(button for button in self.at.button if button.label == "Получить ответ")
        self.assertFalse(button.disabled)
        self.at.text_input(key="api_key").set_value("another-key")
        self.at.text_area(key="question").set_value("Срок гарантии?")
        button.click().run()
        self.client.chat.assert_not_called()
        self.assertEqual(len(self.at.exception), 0)

    def test_replacement_resets_page_and_answer_only_after_preparation(self):
        self.prepare()
        self.ask()
        old_item = self.at.session_state["result"]["item_id"]
        self.at.number_input(key="view_page").set_value(3).run()
        self.upload(self.data + b"\n% second document\n")
        self.assertEqual(self.at.session_state["result"]["item_id"], old_item)
        self.at.button(key="prepare_document").click().run()
        self.assertEqual(len(self.at.exception), 0)
        self.assertIsNone(self.at.session_state["result"])
        self.assertEqual(self.at.session_state["view_page"], 1)
        self.assertNotEqual(self.at.session_state["active_item_id"], old_item)

    def test_restart_keeps_pdf_but_not_key_or_verified_connection(self):
        from streamlit.testing.v1 import AppTest
        self.prepare()
        restarted = AppTest.from_string(self.script, default_timeout=20).run()
        self.assertEqual(len(restarted.exception), 0)
        self.assertEqual(restarted.text_input(key="api_key").value, "")
        self.assertIsNone(restarted.session_state["checked_settings"])
        self.assertEqual(restarted.number_input(key="view_page").value, 1)
        self.assertTrue(any("Cover page" in text.value for text in restarted.text))
        self.client.submit_document.assert_called_once()

    def test_failed_question_removes_previous_answer_and_busy_tab_recovers(self):
        self.prepare()
        self.ask()
        self.client.chat.side_effect = HTTPError("https://host/v1", 503, "session-secret", {}, None)
        self.ask("Новый вопрос")
        self.assertIsNone(self.at.session_state["result"])
        self.assertFalse(any(text.value.startswith("Проверяемый ответ") for text in self.at.text))
        self.assertTrue(any("не ответил" in error.value for error in self.at.error))
        with self.app.operation():
            self.at.run()
        self.assertTrue(any("Уже выполняется операция" in warning.value for warning in self.at.warning))
        self.at.run()
        self.assertEqual(len(self.at.exception), 0)
        self.assertEqual(self.at.number_input(key="view_page").value, 1)


if __name__ == "__main__":
    unittest.main()
