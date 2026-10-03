"""Локальные пути и атомарный манифест. Замок захватывает действие в app.py."""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from reader import AppError, MAX_PDF_BYTES, validate_base_url

LOCK = threading.Lock()


def ensure_safe_path(path: Path) -> Path:
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise AppError("Символические ссылки в папке данных не поддерживаются.")
    return path


def make_item_id(data: bytes, base_url: str, index_model: str, sdk_version: str) -> str:
    profile = {"pdf_sha256": hashlib.sha256(data).hexdigest(),
               "base_url": validate_base_url(base_url),
               "index_model": index_model, "sdk_version": sdk_version}
    if any(not isinstance(value, str) or not value.strip()
           for value in (index_model, sdk_version)):
        raise AppError("Не указан профиль индекса.")
    return hashlib.sha256(
        json.dumps(profile, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def item_dir(data_root: Path, item_id: str) -> Path:
    if not isinstance(item_id, str) or re.fullmatch(r"[0-9a-f]{64}", item_id) is None:
        raise AppError("Некорректный идентификатор документа.")
    path = ensure_safe_path(Path(data_root) / "items" / item_id)
    if path.exists() and not path.is_dir():
        raise AppError("Папка документа повреждена.")
    if path.is_dir() and any(child.is_symlink() for child in path.rglob("*")):
        raise AppError("Символические ссылки в папке данных не поддерживаются.")
    return path


def atomic_write_json(path: Path, value: dict) -> None:
    path = ensure_safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None  # Файл уже стал манифестом; после commit нечего удалять.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_active(data_root: Path) -> dict | None:
    try:
        path = ensure_safe_path(Path(data_root) / "active.json")
        if not path.exists():
            return None
        active = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(active, dict)
                or set(active) != {"version", "item_id", "display_name", "sdk_doc_id",
                                   "page_count", "index_profile"}
                or type(active["version"]) is not int or active["version"] != 1
                or type(active["page_count"]) is not int or not 1 <= active["page_count"] <= 500
                or any(not isinstance(active[field], str) or not active[field].strip()
                       for field in ("display_name", "sdk_doc_id"))):
            raise ValueError("manifest")
        profile = active["index_profile"]
        if (not isinstance(profile, dict)
                or set(profile) != {"base_url", "index_model", "sdk_version"}
                or any(not isinstance(value, str) or not value.strip() for value in profile.values())
                or validate_base_url(profile["base_url"]) != profile["base_url"]):
            raise ValueError("profile")
        pdf_path = item_dir(data_root, active["item_id"]) / "source.pdf"
        if not pdf_path.is_file() or not 0 < pdf_path.stat().st_size <= MAX_PDF_BYTES:
            raise ValueError("PDF")
        if make_item_id(pdf_path.read_bytes(), **profile) != active["item_id"]:
            raise ValueError("profile does not match PDF")
        return active
    except (OSError, ValueError, TypeError, KeyError, AppError) as error:
        raise AppError(
            "Сохранённый документ повреждён или недоступен; выберите PDF заново. Данные не удалены."
        ) from error
