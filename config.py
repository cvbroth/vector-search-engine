"""Explicit, local-only knowledge scopes and shared indexing settings."""

from __future__ import annotations

import logging
import math
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class KnowledgeScope:
    name: str
    area: str

    def __post_init__(self) -> None:
        if (self.name, self.area) not in {("chen", "private"), ("family", "shared")}:
            raise ValueError("unsupported knowledge scope layout")

    @property
    def source_dir(self) -> Path:
        return Path("/srv/storage/knowledge") / self.area / self.name

    @property
    def state_dir(self) -> Path:
        return Path("/var/lib/knowledge-base") / self.area / self.name

    @property
    def database_path(self) -> Path:
        return self.state_dir / "index" / "knowledge.db"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"


DEFAULT_SCOPE = "chen"
KNOWLEDGE_SCOPES: Mapping[str, KnowledgeScope] = MappingProxyType(
    {
        "chen": KnowledgeScope("chen", "private"),
        "family": KnowledgeScope("family", "shared"),
    }
)

EMBEDDING_URL = "http://127.0.0.1:19433/v1/embeddings"
EMBEDDING_MODEL = "EmbeddingGemma 300M"
EMBEDDING_DIMENSION = 768
EMBEDDING_BATCH_SIZE = 16
EMBEDDING_TIMEOUT_SECONDS = 45

# Provisional values from a small labeled calibration set; not answerability proof.
RELEVANCE_REJECT_THRESHOLD = 0.55
RELEVANCE_ACCEPT_THRESHOLD = 0.64

CHUNK_MAX_CHARS = 1200
CHUNK_OVERLAP_CHARS = 160
SUPPORTED_SUFFIXES = frozenset({".md", ".txt", ".pdf", ".docx"})


def validate_relevance_thresholds(reject: float, accept: float) -> None:
    """Reject invalid or reversed cosine-similarity decision boundaries."""
    if type(reject) not in (int, float) or type(accept) not in (int, float):
        raise ValueError("relevance thresholds must satisfy -1 <= reject < accept <= 1")
    try:
        valid = (
            math.isfinite(reject)
            and math.isfinite(accept)
            and -1.0 <= reject < accept <= 1.0
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("relevance thresholds must satisfy -1 <= reject < accept <= 1")


validate_relevance_thresholds(
    RELEVANCE_REJECT_THRESHOLD, RELEVANCE_ACCEPT_THRESHOLD
)


def get_scope(name: str) -> KnowledgeScope:
    """Only two hard-coded layouts are available; callers cannot pass a path."""
    try:
        scope = KNOWLEDGE_SCOPES[name]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unsupported knowledge scope: {name!r}") from exc
    if scope.name != name:
        raise ValueError(f"misconfigured knowledge scope: {name!r}")
    return scope


def reject_symlink_components(path: Path) -> None:
    """Reject traversal and any existing symlink component without following it."""
    if not path.is_absolute() or ".." in path.parts:
        raise OSError(f"unsafe path: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise OSError(f"symlink in configured path: {current}")


def configure_logging(scope: KnowledgeScope, filename: str) -> None:
    """Log only inside the chosen scope's fixed state directory."""
    if filename not in {"ingest.log", "search.log"}:
        raise ValueError("unsupported log filename")
    log_path = scope.log_dir / filename
    reject_symlink_components(log_path)
    scope.log_dir.mkdir(parents=True, exist_ok=True)
    reject_symlink_components(log_path)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
