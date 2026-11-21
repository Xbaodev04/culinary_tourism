"""Deterministic provenance of audio translations derived from Vietnamese POI text."""
import hashlib


def source_fingerprint(name: str, description: str) -> str:
    # This is a freshness identifier, NOT an authentication/security digest.
    return hashlib.sha256(((name or "") + "\0" + (description or "")).encode("utf-8")).hexdigest()


def text_fingerprint(text: str) -> str:
    """Independent provenance for a title or narration, without POI metadata."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def field_is_current(localization: dict, field: str, name: str, description: str) -> bool:
    specific = localization.get(f"{field}_source_hash")
    if specific is not None:
        return specific == text_fingerprint(name if field == "title" else description)
    # Lazily upgrade legacy records, but never trust a record without provenance.
    return localization.get("source_hash") == source_fingerprint(name, description)
