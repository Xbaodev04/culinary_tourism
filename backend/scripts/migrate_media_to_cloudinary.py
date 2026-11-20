"""Migrate referenced local images/MP3s; dry-run unless --apply is supplied.

Run from backend/ on the machine containing both the files and a full checkout.
Uses the database selected by MONGO_URI/DB_NAME. Never deletes original files.
"""
import argparse
import asyncio
import re

from app.core import database, media_storage
from app.core.config import settings
from app.core.path_security import resolve_safe_path
from app.modules.owner.service import _store_image

COLLECTIONS = ("poi", "menu_items", "poi_owner_registrations", "poi_submissions", "poi_localizations")


def local_urls(value):
    if isinstance(value, str) and value.startswith(("/static/images/", "/static/audio/")):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from local_urls(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from local_urls(nested)


def replace_urls(value, replacements):
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, dict):
        return {key: replace_urls(nested, replacements) for key, nested in value.items()}
    if isinstance(value, list):
        return [replace_urls(nested, replacements) for nested in value]
    return value


async def migrate(*, apply: bool = False) -> dict:
    if apply:
        if settings.MEDIA_STORAGE != "cloudinary":
            raise RuntimeError("Set MEDIA_STORAGE=cloudinary before applying migration")
        media_storage.validate_media_config()
    replacements = {}
    missing = set()
    summary = {"references": 0, "uploaded": 0, "missing": 0, "updated_documents": 0, "conflicts": 0}
    for name in COLLECTIONS:
        collection = database.get_db()[name]
        async for doc in collection.find({}):
            urls = set(local_urls(doc))
            summary["references"] += len(urls)
            for url in sorted(urls):
                if url in replacements or url in missing:
                    continue
                relative = url.removeprefix("/static/")
                # Strict checks also reject traversal and symlinks; unsafe records stop migration.
                path = resolve_safe_path(settings.static_path, relative)
                if not path.is_file() or path.stat().st_size == 0:
                    missing.add(url)
                    continue
                if relative.startswith("audio/"):
                    if not re.fullmatch(r"audio/[0-9a-f]{32}\.mp3", relative):
                        raise ValueError("Unsupported local audio filename")
                    if apply:
                        replacements[url] = (await media_storage.upload_audio(path, path.stem)).url
                else:
                    # Legacy images directly below images/ are placed in the poi folder.
                    folder = relative.split("/")[1] if len(relative.split("/")) > 2 else "poi"
                    if folder not in {"poi", "menu", "storefront"}:
                        raise ValueError("Unsupported local image folder")
                    if apply:
                        replacements[url] = await _store_image(path.read_bytes(), folder)
            if apply:
                changes = {key: replace_urls(value, replacements) for key, value in doc.items()
                           if key != "_id" and set(local_urls(value)) & replacements.keys()}
                if changes:
                    # Do not overwrite a field that another request edited during upload.
                    guard = {"_id": doc["_id"], **{key: doc[key] for key in changes}}
                    result = await collection.update_one(guard, {"$set": changes})
                    summary["updated_documents" if result.matched_count else "conflicts"] += 1
    summary["uploaded"] = len(replacements)
    summary["missing"] = len(missing)
    return summary


async def main(apply: bool):
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        await client.admin.command("ping")
        database.db = client[settings.DB_NAME]
        print(f"Database: {settings.DB_NAME}; mode: {'APPLY' if apply else 'DRY RUN'}")
        print(await migrate(apply=apply))
    finally:
        database.db = None
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Upload files and update matching database references")
    asyncio.run(main(parser.parse_args().apply))
