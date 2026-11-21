"""Cloudinary uploads with durable Mongo metadata and restricted delivery URLs.

The SDK is synchronous: network calls run outside the event loop. Content hashes
are shared across POIs, so removing a reference must not delete a shared asset.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import logging
from pathlib import Path
import re
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.database import get_db

logger = logging.getLogger("quan4.media")

class MediaStorageError(RuntimeError):
    def __init__(self, message: str, *, code: str = "upload_failed"):
        super().__init__(message)
        self.code = code


def _upload_error(error: Exception) -> MediaStorageError:
    """Translate provider errors to fixed messages; never expose request parameters."""
    from cloudinary.exceptions import AuthorizationRequired, BadRequest, NotAllowed, RateLimited

    message = str(error).lower()
    if "disabled" in message or "suspended" in message:
        code, detail = "account_disabled", "Tài khoản Cloudinary đang bị vô hiệu hóa hoặc tạm ngưng."
    elif isinstance(error, AuthorizationRequired) or any(
        marker in message for marker in ("invalid api_key", "unknown api_key", "invalid signature", "api_secret mismatch")
    ):
        code, detail = "authentication_failed", "Dịch vụ lưu media chưa xác thực được. Vui lòng liên hệ quản trị viên."
    elif isinstance(error, RateLimited) or any(
        marker in message for marker in ("quota", "credit limit", "usage limit", "rate limit")
    ):
        code, detail = "quota_exceeded", "Cloudinary đã giới hạn yêu cầu hoặc hết hạn mức lưu trữ/xử lý."
    elif isinstance(error, NotAllowed):
        code, detail = "upload_not_allowed", "Dịch vụ lưu media không cho phép tải file. Vui lòng liên hệ quản trị viên."
    elif isinstance(error, BadRequest) and any(
        marker in message for marker in ("invalid image", "image file", "unsupported", "file size", "too large")
    ):
        code, detail = "invalid_media", "Cloudinary không chấp nhận file này. Thử ảnh JPEG, PNG hoặc WebP khác dưới 5 MB."
    elif isinstance(error, BadRequest):
        code, detail = "invalid_upload_request", "Dịch vụ lưu media từ chối yêu cầu tải file. Vui lòng liên hệ quản trị viên."
    elif any(marker in message for marker in ("timeout", "timed out", "socket error", "ssl", "connection", "certificate")):
        code, detail = "connection_failed", "Backend không kết nối được Cloudinary hoặc upload quá thời gian. Vui lòng thử lại."
    else:
        code, detail = "upload_failed", "Không tải được media lên Cloudinary. Kiểm tra cấu hình và hạn mức."
    return MediaStorageError(detail, code=code)


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
    except Exception as error:
        # Provider exceptions may include signed request parameters or secrets.
        failure = _upload_error(error)
        logger.warning("Cloudinary upload failed code=%s resource_type=%s", failure.code, resource_type)
        raise failure from None
    url = result.get("secure_url") if isinstance(result, dict) else None
    if (not isinstance(result, dict) or result.get("public_id") != public_id or result.get("resource_type") != resource_type
            or not _valid_delivery_url(url, resource_type, public_id)):
        logger.warning("Cloudinary upload failed code=invalid_response resource_type=%s", resource_type)
        raise MediaStorageError("Cloudinary trả về thông tin media không hợp lệ", code="invalid_response")
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
