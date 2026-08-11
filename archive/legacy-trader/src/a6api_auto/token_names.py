from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Sequence
from functools import lru_cache
from importlib.resources import files

# Lower-case Crockford base32 avoids visually ambiguous characters. Requiring a
# letter first keeps generated names from being mistaken for numeric identifiers.
TOKEN_NAME_INITIAL_ALPHABET = "abcdefghjkmnpqrstvwxyz"
TOKEN_NAME_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
TOKEN_NAME_LENGTH = 24
LEGACY_TOKEN_NAME_PATTERN = re.compile(
    rf"^[{TOKEN_NAME_INITIAL_ALPHABET}][{TOKEN_NAME_ALPHABET}]"
    rf"{{{TOKEN_NAME_LENGTH - 1}}}$"
)
TOKEN_NAME_PATTERN = LEGACY_TOKEN_NAME_PATTERN
CORPUS_TOKEN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
TOKEN_NAME_CORPUS_RESOURCE = "data/token_name_corpus.json"
IDENTITY_LINKED_TOKEN_PREFIXES = ("merchant-",)


@lru_cache(maxsize=1)
def token_name_corpus() -> tuple[str, ...]:
    resource = files("a6api_auto").joinpath(TOKEN_NAME_CORPUS_RESOURCE)
    try:
        document = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("token_name_corpus_unreadable") from exc
    names = document.get("names") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or not isinstance(names, list)
    ):
        raise RuntimeError("token_name_corpus_invalid")
    corpus = tuple(names)
    if (
        len(corpus) < 100
        or len(set(corpus)) != len(corpus)
        or any(
            not isinstance(value, str)
            or value != value.strip()
            or CORPUS_TOKEN_NAME_PATTERN.fullmatch(value) is None
            for value in corpus
        )
    ):
        raise RuntimeError("token_name_corpus_invalid")
    return corpus


def generate_anonymous_token_name(
    *,
    choice: Callable[[Sequence[str]], str] | None = None,
) -> str:
    """Select a generic AI/coding token label containing no account-derived data."""

    selector = choice or secrets.choice
    corpus = token_name_corpus()
    value = selector(corpus)
    if value not in corpus:
        raise ValueError("anonymous_token_name_entropy_source_invalid")
    return value


def is_anonymous_token_name(value: object) -> bool:
    return isinstance(value, str) and (
        value in token_name_corpus() or LEGACY_TOKEN_NAME_PATTERN.fullmatch(value) is not None
    )


def is_identity_linked_token_name(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold()
    return any(normalized.startswith(prefix) for prefix in IDENTITY_LINKED_TOKEN_PREFIXES)
