from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import requests

from app.core.config import settings


class StorageError(RuntimeError):
    pass


def using_supabase_storage() -> bool:
    return (
        settings.storage_backend.lower() == "supabase"
        and bool(settings.supabase_url)
        and bool(settings.supabase_service_role_key)
    )


def _object_url(bucket: str, object_path: str) -> str:
    base_url = settings.supabase_url.rstrip("/")
    encoded_path = "/".join(quote(part) for part in object_path.split("/"))
    return f"{base_url}/storage/v1/object/{bucket}/{encoded_path}"


def public_object_url(bucket: str, object_path: str) -> str:
    base_url = settings.supabase_url.rstrip("/")
    encoded_path = "/".join(quote(part) for part in object_path.split("/"))
    return f"{base_url}/storage/v1/object/public/{bucket}/{encoded_path}"


def upload_object(
    *,
    bucket: str,
    object_path: str,
    content: bytes,
    content_type: str | None = None,
    public: bool = False,
) -> str:
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
        "x-upsert": "true",
    }
    if content_type:
        headers["Content-Type"] = content_type

    try:
        response = requests.post(
            _object_url(bucket, object_path),
            headers=headers,
            data=content,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise StorageError("Could not upload file to Supabase Storage.") from exc

    if public:
        return public_object_url(bucket, object_path)
    return f"supabase://{bucket}/{object_path}"


def delete_object_url(value: str | None) -> None:
    if not value or not using_supabase_storage():
        return

    base_public = (
        f"{settings.supabase_url.rstrip('/')}/storage/v1/object/public/"
        if settings.supabase_url
        else ""
    )
    bucket = None
    object_path = None

    if value.startswith("supabase://"):
        bucket_and_path = value.removeprefix("supabase://")
        bucket, _, object_path = bucket_and_path.partition("/")
    elif base_public and value.startswith(base_public):
        bucket_and_path = value.removeprefix(base_public)
        bucket, _, object_path = bucket_and_path.partition("/")

    if not bucket or not object_path:
        return

    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
    }

    try:
        requests.delete(
            _object_url(bucket, object_path),
            headers=headers,
            timeout=30,
        ).raise_for_status()
    except requests.RequestException:
        return


def save_local_file(directory: Path, filename: str, content: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(content)
    return path
