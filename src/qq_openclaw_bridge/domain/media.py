from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


class MediaRejected(ValueError):
    pass


@dataclass(frozen=True)
class MediaLimits:
    image_bytes: int = 10 * 1024 * 1024
    audio_bytes: int = 20 * 1024 * 1024
    file_bytes: int = 20 * 1024 * 1024
    max_images: int = 4

    def for_kind(self, kind: str) -> int:
        limits = {
            "image": self.image_bytes,
            "audio": self.audio_bytes,
            "file": self.file_bytes,
        }
        try:
            return limits[kind]
        except KeyError as error:
            raise MediaRejected("unsupported media kind") from error


@dataclass(frozen=True)
class ValidatedMedia:
    asset_id: str
    path: Path
    kind: str
    mime: str
    size: int
    sha256: str
    expires_at: Optional[datetime]


@dataclass(frozen=True)
class DocumentArtifact:
    text: str
    mime: str
    truncated: bool = False


__all__ = [
    "DocumentArtifact",
    "MediaLimits",
    "MediaRejected",
    "ValidatedMedia",
]
