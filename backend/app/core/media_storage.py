"""Cloudinary uploads with durable Mongo metadata and restricted delivery URLs.

The SDK is synchronous: network calls run outside the event loop. Content hashes
are shared across POIs, so removing a reference must not delete a shared asset.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.database import get_db


class MediaStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudinaryAsset:
    url: str
    public_id: str
    resource_type: str
    size_bytes: int | None = None

    def download_url(self, filename: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", filename):
            raise ValueError("Invalid download filename")
        return self.url.replace("/video/upload/", f"/video/upload/fl_attachment:{filename}/", 1)


def validate_media_config() -> None:
    if settings.MEDIA_STORAGE != "cloudinary":
        return
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", settings.CLOUDINARY_CLOUD_NAME):
        raise RuntimeError("CLOUDINARY_CLOUD_NAME is missing or invalid")
    if not settings.CLOUDINARY_API_KEY.strip() or not settings.CLOUDINARY_API_SECRET.strip():
        raise RuntimeError("Cloudinary requires CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", settings.CLOUDINARY_FOLDER):
        raise RuntimeError("CLOUDINARY_FOLDER must contain safe folder names")


def asset_id(resource_type: str, public_id: str) -> str:
    return f"cloudinary:{settings.CLOUDINARY_CLOUD_NAME}:{resource_type}:{public_id}"


def audio_public_id(digest: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", digest):
        raise ValueError("Invalid audio digest")
    return f"{settings.CLOUDINARY_FOLDER}/audio/{digest}"


def _valid_delivery_url(url: str, resource_type: str, public_id: str) -> bool:
    """Accept only untransformed URLs for this cloud and the expected asset."""
    if not isinstance(url, str) or not settings.CLOUDINARY_CLOUD_NAME:
        return False
    if any(ord(char) < 33 for char in url) or "\\" in url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if (parsed.scheme != "https" or parsed.netloc != "res.cloudinary.com"
            or parsed.query or parsed.fragment):
        return False
    prefix = f"/{settings.CLOUDINARY_CLOUD_NAME}/{resource_type}/upload/"
    if not parsed.path.startswith(prefix):
        return False
    path = re.sub(r"^v[0-9]+/", "", parsed.path[len(prefix):])
    extensions = "mp3" if resource_type == "video" else "jpg|jpeg|png|webp"
    return re.fullmatch(re.escape(public_id) + rf"\.(?:{extensions})", path) is not None


async def cached_asset(resource_type: str, public_id: str) -> CloudinaryAsset | None:
    doc = await get_db()["media_assets"].find_one({"_id": asset_id(resource_type, public_id)})
    if doc and _valid_delivery_url(doc.get("secure_url"), resource_type, public_id):
        return CloudinaryAsset(doc["secure_url"], public_id, resource_type, doc.get("bytes"))
    return None


async def cloudinary_audio(url: str) -> CloudinaryAsset | None:
    """Resolve only registered MP3s, never arbitrary URLs from database fields."""
    if not isinstance(url, str) or not settings.CLOUDINARY_CLOUD_NAME:
        return None
    match = re.search(r"/([0-9a-f]{32})\.mp3$", url)
    if not match:
        return None
    public_id = audio_public_id(match[1])
    if not _valid_delivery_url(url, "video", public_id):
        return None
    asset = await cached_asset("video", public_id)
    return asset if asset and asset.url == url else None


async def _upload(source: bytes | Path, public_id: str, resource_type: str) -> CloudinaryAsset:
    validate_media_config()
    existing = await cached_asset(resource_type, public_id)
    if existing:
        return existing

    def upload():
        import cloudinary.uploader

        return cloudinary.uploader.upload(
            BytesIO(source) if isinstance(source, bytes) else str(source),
            cloud_name=settings.CLOUDINARY_CLOUD_NAME,
            api_key=settings.CLOUDINARY_API_KEY,
            api_secret=settings.CLOUDINARY_API_SECRET,
            resource_type=resource_type,
            public_id=public_id,
            overwrite=False,
            unique_filename=False,
            type="upload",
            secure=True,
            timeout=60,
        )

    try:
        result = await asyncio.to_thread(upload)
    except Exception:
        # Provider exceptions may include signed request parameters or secrets.
        raise MediaStorageError("Không tải được media lên Cloudinary. Kiểm tra cấu hình và hạn mức.") from None
    url = result.get("secure_url")
    if (result.get("public_id") != public_id or result.get("resource_type") != resource_type
            or not _valid_delivery_url(url, resource_type, public_id)):
        raise MediaStorageError("Cloudinary trả về thông tin media không hợp lệ")
    await get_db()["media_assets"].update_one(
        {"_id": asset_id(resource_type, public_id)},
        {"$set": {"provider": "cloudinary", "cloud_name": settings.CLOUDINARY_CLOUD_NAME,
                  "public_id": public_id, "resource_type": resource_type,
                  "secure_url": url, "bytes": result.get("bytes"),
                  "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return CloudinaryAsset(url, public_id, resource_type, result.get("bytes"))


async def upload_image(content: bytes, folder: str, digest: str) -> CloudinaryAsset:
    if folder not in {"poi", "menu", "storefront"} or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid image identity")
    return await _upload(content, f"{settings.CLOUDINARY_FOLDER}/images/{folder}/{digest}", "image")


async def upload_audio(file: Path, digest: str) -> CloudinaryAsset:
    return await _upload(file, audio_public_id(digest), "video")
