"""Один пользователь, один процесс: все действия выполняются по кнопке."""

from contextlib import contextmanager
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil

import streamlit as st

import reader
import storage

PROJECT = Path(__file__).resolve().parent
DATA_ROOT = PROJECT / "data"
SDK_VERSION = version("pageindex")


@contextmanager
def operation():
    if not storage.LOCK.acquire(blocking=False):
        raise reader.AppError("Уже выполняется операция. Дождитесь завершения")
    try:
        yield
    finally:
        storage.LOCK.release()


def settings_fingerprint(settings: dict) -> tuple:
    return tuple(settings.get(key, "") for key in ("base_url", "api_key", "index_model", "chat_model"))


def index_profile(settings: dict) -> dict:
    return {"base_url": settings["base_url"], "index_model": settings["index_model"],
            "sdk_version": SDK_VERSION}


def sync_state(state, active: dict | None, settings: dict) -> None:
    fingerprint = settings_fingerprint(settings)
    if state.get("settings_fingerprint") != fingerprint:
        state["settings_fingerprint"] = fingerprint
        state["checked_settings"] = None
        state["result"] = None
    item_id = active["item_id"] if active else None
    if state.get("active_item_id") != item_id:
        state["active_item_id"] = item_id
        state["result"] = None
        state["view_page"] = 1
    if state.get("result") and state["result"].get("item_id") != item_id:
        state["result"] = None


def select_page(number: int) -> None:
    st.session_state["view_page"] = number


def verify_connection(data_root: Path, settings: dict) -> None:
    with operation():
        settings = reader.model_settings(**settings)
        reader.check_connection(**settings)
        stored = {key: settings[key] for key in ("base_url", "index_model", "chat_model")}
        stored["is_local"] = reader.is_local_url(settings["base_url"])
        storage.atomic_write_json(data_root / "settings.json", stored)


def prepare_document(data_root: Path, data: bytes, name: str, settings: dict, progress=None) -> tuple:
    with operation():
        folder, created, committed, created_source = None, False, False, False
        notify = progress or (lambda label: None)
        try:
            notify("Проверяю PDF")
            inspected = reader.inspect_pdf(data, name)
            settings = reader.model_settings(**settings)
            profile = index_profile(settings)
            item_id = storage.make_item_id(data, **profile)
            folder = storage.item_dir(data_root, item_id)
            try:
                previous = storage.load_active(data_root)
            except reader.AppError:
                # Повреждённый манифест сохраняется до успешной подготовки нового PDF.
                previous = None
            warnings = []
            if inspected["empty_text_pages"]:
                warnings.append(
                    f"Страниц без текста: {len(inspected['empty_text_pages'])}. "
                    "Поиск охватывает текстовый слой и не читает изображения."
                )
            if previous and previous["item_id"] == item_id:
                notify("Проверяю готовый индекс")
                client = reader.make_client(**settings, storage_path=folder / "index")
                reader.check_index(client, previous["sdk_doc_id"], inspected["page_count"])
                manifest = {**previous, "display_name": name}
                storage.atomic_write_json(data_root / "active.json", manifest)
                notify("Документ готов")
                return manifest, warnings
            doc_id = None
            source = folder / "source.pdf"
            if folder.exists():
                notify("Проверяю готовый индекс")
                if not (folder / "index").is_dir() or (source.exists() and source.read_bytes() != data):
                    raise reader.AppError(
                        f"Данные подготовки повреждены. Сохраните папку data/items/{item_id} "
                        "в другом месте и выберите PDF заново."
                    )
                client = reader.make_client(**settings, storage_path=folder / "index")
                with reader.quiet_sdk():
                    library = client.list_documents(limit=2)
                documents = library.get("documents") if isinstance(library, dict) else None
                if (not isinstance(documents, list) or len(documents) != 1
                        or type(library.get("total")) is not int or library["total"] != 1
                        or not isinstance(documents[0], dict) or documents[0].get("name") != "source.pdf"
                        or not isinstance(documents[0].get("id"), str) or not documents[0]["id"].strip()):
                    raise reader.AppError("Не удалось восстановить готовый индекс. Данные сохранены в data/items.")
                doc_id = documents[0]["id"]
                reader.check_index(client, doc_id, inspected["page_count"])
            else:
                folder.mkdir(parents=True)
                created = True
            if not source.exists():
                with source.open("xb") as handle:
                    created_source = True
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            if doc_id is None:
                notify("Создаю индекс")
                client = reader.make_client(**settings, storage_path=folder / "index")
                doc_id = reader.index_pdf(client, source, inspected["page_count"])
            manifest = {"version": 1, "item_id": item_id, "display_name": name,
                        "sdk_doc_id": doc_id, "page_count": inspected["page_count"],
                        "index_profile": profile}
            storage.atomic_write_json(data_root / "active.json", manifest)
            committed = True
            if previous:
                try:
                    shutil.rmtree(storage.item_dir(data_root, previous["item_id"]))
                except (OSError, reader.AppError):
                    warnings.append("Документ готов, но прежнюю папку удалить не удалось. Она сохранена в data/items.")
            notify("Документ готов")
            return manifest, warnings
        except Exception as error:
            if created and not committed:
                try:
                    shutil.rmtree(storage.item_dir(data_root, folder.name))
                except (OSError, reader.AppError):
                    pass  # Удаляем только папку, созданную этой попыткой; активную не трогаем.
            elif created_source and not committed:
                try:
                    storage.ensure_safe_path(folder / "source.pdf").unlink(missing_ok=True)
                except (OSError, reader.AppError):
                    pass
            raise reader.AppError(reader.error_message(error)) from error


def answer_question(data_root: Path, expected_item_id: str, settings: dict, question: str) -> dict:
    with operation():
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise reader.AppError("Введите вопрос длиной от 1 до 2000 символов.")
        active = storage.load_active(data_root)
        if not active or active["item_id"] != expected_item_id:
            raise reader.AppError("Документ изменён в другой вкладке. Обновите страницу и повторите вопрос.")
        settings = reader.model_settings(**settings)
        if active["index_profile"] != index_profile(settings):
            raise reader.AppError("Настройки индекса изменились. Подготовьте документ с новым профилем.")
        folder = storage.item_dir(data_root, active["item_id"])
        client = reader.make_client(**settings, storage_path=folder / "index")
        reader.check_index(client, active["sdk_doc_id"], active["page_count"])
        result = reader.ask_pdf(client, active["sdk_doc_id"], active["page_count"], question)
        return {"item_id": active["item_id"], "question": question.strip(), **result}


def load_settings(data_root: Path) -> dict:
    path = storage.ensure_safe_path(data_root / "settings.json")
    if not path.exists():
        return {}
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(stored, dict)
                or set(stored) != {"base_url", "index_model", "chat_model", "is_local"}
                or type(stored["is_local"]) is not bool
                or any(not isinstance(stored[key], str) or not stored[key].strip()
                       for key in ("base_url", "index_model", "chat_model"))
                or reader.validate_base_url(stored["base_url"]) != stored["base_url"]
                or reader.is_local_url(stored["base_url"]) != stored["is_local"]):
            raise ValueError("settings")
        return {key: stored[key] for key in ("base_url", "index_model", "chat_model")}
    except (OSError, ValueError, KeyError, reader.AppError) as error:
        raise reader.AppError("Не удалось прочитать настройки. Введите их заново.") from error


def main():
    st.set_page_config(page_title="PDF Desk", layout="wide", page_icon="📄")
    st.title("PDF Desk")
    st.text("Задайте вопрос к PDF и проверьте ответ по странице рядом.")
    st.info(
        "PDF и индекс сохраняются на этом компьютере. Для подготовки документа и ответов "
        "текст передаётся выбранному серверу модели. При внешнем API запросы могут быть платными."
    )
    state = st.session_state
    if "settings_loaded" not in state:
        try:
            with operation():
                saved = load_settings(DATA_ROOT)
        except Exception as error:
            saved = {}
            st.warning(reader.error_message(error))
        for key, value in {"base_url": "http://127.0.0.1:11434/v1",
                           "index_model": "", "chat_model": "", **saved}.items():
            state.setdefault(key, value)
        state.setdefault("api_key", "")
        state["settings_loaded"] = True

    with st.sidebar:
        st.header("Подключение модели")
        # Поля вне st.form: изменение сразу сбрасывает проверку и прежний ответ.
        st.text_input("Адрес API", key="base_url", help="Адрес HTTP(S), оканчивающийся на /v1.")
        st.text_input("Модель для подготовки документа", key="index_model")
        st.text_input("Модель для ответов", key="chat_model")
        st.text_input("API-ключ", key="api_key", type="password",
                      help="Хранится только в этой сессии. Для локального сервера без авторизации оставьте пустым.")
        check_clicked = st.button("Проверить подключение", key="check_connection")

    raw_settings = {key: state[key] for key in ("base_url", "api_key", "index_model", "chat_model")}
    try:
        with operation():
            active = storage.load_active(DATA_ROOT)
    except Exception as error:
        active = None
        st.warning(reader.error_message(error))
        if storage.LOCK.locked():
            st.button("Обновить", key="refresh_busy")
            return
    sync_state(state, active, raw_settings)
    try:
        settings = reader.model_settings(**raw_settings)
    except reader.AppError:
        settings = None

    if check_clicked:
        state["checked_settings"] = None
        state["result"] = None
        try:
            with st.spinner("Проверяю модели и вызов инструмента…"):
                verify_connection(DATA_ROOT, raw_settings)
            state["checked_settings"] = settings_fingerprint(raw_settings)
        except Exception as error:
            st.sidebar.error(reader.error_message(error))
    verified = bool(settings and state.get("checked_settings") == settings_fingerprint(raw_settings))
    if verified:
        st.sidebar.success("Подключение работает")
    else:
        st.sidebar.info("Введите настройки и проверьте подключение перед отправкой текста модели.")

    st.subheader("Выберите документ")
    uploaded = st.file_uploader("PDF-документ", type=["pdf"], accept_multiple_files=False,
                                max_upload_size=25, key="uploaded_pdf")
    st.caption("До 25 МиБ и 500 страниц. Нужен текстовый слой; сканы, картинки и сложные таблицы не поддерживаются.")
    if st.button("Подготовить документ", disabled=uploaded is None or not verified, key="prepare_document"):
        state["preparation_warnings"] = []
        with st.status("Проверяю PDF", expanded=True) as status:
            try:
                active, warnings = prepare_document(
                    DATA_ROOT, uploaded.getvalue(), uploaded.name, settings,
                    progress=lambda label: status.update(label=label),
                )
                sync_state(state, active, raw_settings)
                state["result"] = None
                state["preparation_warnings"] = warnings
                status.update(label="Документ готов", state="complete", expanded=False)
            except Exception as error:
                status.update(label="Документ не заменён", state="error")
                st.error(reader.error_message(error))
    for warning in state.get("preparation_warnings", []):
        st.warning(warning)

    if active is None:
        st.text("Выберите PDF и нажмите «Подготовить документ».")
        return
    st.subheader("Активный документ")
    st.text(active["display_name"])
    matching_profile = bool(settings and active["index_profile"] == index_profile(settings))
    if settings and not matching_profile:
        st.warning(
            "Адрес сервера, модель индекса или версия SDK изменились. "
            "Подготовьте PDF заново. Страницы доступны для просмотра."
        )
    left, right = st.columns([1, 1])
    with left:
        with st.form("question_form"):
            question = st.text_area("Вопрос к документу", key="question", max_chars=2000,
                                    placeholder="Например: какой срок гарантии?")
            ask_clicked = st.form_submit_button("Получить ответ", disabled=not (verified and matching_profile))
        answer_slot = st.empty()
        if ask_clicked:
            state["result"] = None
            answer_slot.empty()
            try:
                with st.spinner("Ищу ответ в документе…"):
                    state["result"] = answer_question(DATA_ROOT, active["item_id"], settings, question)
            except Exception as error:
                st.error(reader.error_message(error))
        result = state.get("result")
        if result and result.get("item_id") == active["item_id"]:
            with answer_slot.container():
                st.subheader("Ответ")
                st.text(result["question"])
                st.text(result["text"])
                for warning in result["warnings"]:
                    st.warning(warning)
                for number, source in enumerate(result["sources"], 1):
                    st.button(f"Источник {number} · страница {source['page']}",
                              key=f"source-{result['item_id']}-{source['page']}",
                              on_click=select_page, args=(source["page"],))
        st.caption("Модель может ошибиться. Проверьте утверждения по странице источника")

    with right:
        st.subheader("Страница документа")
        state.setdefault("view_page", 1)
        st.number_input("Страница PDF", min_value=1, max_value=active["page_count"],
                        step=1, key="view_page")
        page_number = state["view_page"]
        previous, following = st.columns(2)
        previous.button("Предыдущая", disabled=page_number == 1, on_click=select_page, args=(page_number - 1,))
        following.button("Следующая", disabled=page_number == active["page_count"],
                         on_click=select_page, args=(page_number + 1,))
        try:
            with operation():
                current = storage.load_active(DATA_ROOT)
                if not current or current["item_id"] != active["item_id"]:
                    state["result"] = None
                    st.rerun()
                pdf_path = storage.item_dir(DATA_ROOT, active["item_id"]) / "source.pdf"
                image, text = reader.read_page(pdf_path, page_number)
            try:
                st.image(image, width="stretch", caption=f"Страница PDF {page_number} из {active['page_count']}")
            finally:
                image.close()
            with st.expander("Извлечённый текст страницы"):
                st.text(text if text.strip() else "На этой странице нет читаемого текста. Поиск не читает изображения.")
        except Exception as error:
            st.error(reader.error_message(error))


if __name__ == "__main__":
    main()
