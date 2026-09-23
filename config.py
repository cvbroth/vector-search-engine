"""Fixed paths and small, local-only settings for Chen's knowledge base."""

from __future__ import annotations

import logging
from pathlib import Path

SOURCE_ROOT = Path("/srv/storage/knowledge/private/chen")
DATABASE_PATH = Path("/var/lib/knowledge-base/private/chen/index/knowledge.db")
LOG_DIR = Path("/var/lib/knowledge-base/private/chen/logs")

EMBEDDING_URL = "http://127.0.0.1:19433/v1/embeddings"
EMBEDDING_MODEL = "EmbeddingGemma 300M"
EMBEDDING_DIMENSION = 768
EMBEDDING_BATCH_SIZE = 16
EMBEDDING_TIMEOUT_SECONDS = 45

CHUNK_MAX_CHARS = 1200
CHUNK_OVERLAP_CHARS = 160
SUPPORTED_SUFFIXES = frozenset({".md", ".txt", ".pdf", ".docx"})


def configure_logging(filename: str) -> None:
    """Log to the fixed private log directory and to stderr."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / filename, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
