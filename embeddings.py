"""Client for the existing loopback-only OpenAI-compatible embedding service."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Sequence

from config import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_TIMEOUT_SECONDS,
    EMBEDDING_URL,
)


class EmbeddingError(Exception):
    """The local embedding service failed or returned invalid data."""


def _one_batch(texts: Sequence[str]) -> list[list[float]]:
    payload = json.dumps(
        {"model": EMBEDDING_MODEL, "input": list(texts)}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        EMBEDDING_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    # Ignore environment proxy variables even if they happen to be configured.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=EMBEDDING_TIMEOUT_SECONDS) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise EmbeddingError(f"local embedding service returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EmbeddingError(f"local embedding service unavailable: {exc}") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise EmbeddingError("embedding response is not valid JSON") from exc

    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise EmbeddingError("embedding response has no data list")
    ordered: list[list[float] | None] = [None] * len(texts)
    if len(result["data"]) != len(texts):
        raise EmbeddingError("embedding response count does not match request")
    for item in result["data"]:
        if not isinstance(item, dict):
            raise EmbeddingError("embedding response item is not an object")
        index = item.get("index")
        raw = item.get("embedding")
        if type(index) is not int or not 0 <= index < len(texts) or ordered[index] is not None:
            raise EmbeddingError("embedding response has invalid or duplicate index")
        if not isinstance(raw, list) or len(raw) != EMBEDDING_DIMENSION:
            raise EmbeddingError(
                f"embedding has wrong dimension; expected {EMBEDDING_DIMENSION}"
            )
        if any(type(value) not in (int, float) for value in raw):
            raise EmbeddingError("embedding contains a non-numeric value")
        try:
            vector = [float(value) for value in raw]
        except (OverflowError, ValueError) as exc:
            raise EmbeddingError("embedding contains a value outside float range") from exc
        if any(not math.isfinite(value) or abs(value) > 3.4028235e38 for value in vector):
            raise EmbeddingError("embedding contains a non-finite float32 value")
        if not any(vector):
            raise EmbeddingError("embedding is the zero vector")
        ordered[index] = vector
    if any(vector is None for vector in ordered):
        raise EmbeddingError("embedding response is missing an index")
    return [vector for vector in ordered if vector is not None]


def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Batch input while preserving input order; never load a model in-process."""
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise EmbeddingError("embedding input must contain non-empty strings")
    vectors: list[list[float]] = []
    for offset in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        vectors.extend(_one_batch(texts[offset : offset + EMBEDDING_BATCH_SIZE]))
    return vectors
