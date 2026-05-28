from __future__ import annotations

import mimetypes
import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlparse

_FILE_URI_RE = re.compile(r"file://(?:(?:%[0-9A-Fa-f]{2})|[^\s'\"<>])+")


def dropped_file_paths_from_text(
    text: str,
    *,
    current_working_directory: str | Path,
) -> list[Path]:
    """Return existing file paths from text emitted by terminal file drops."""
    paths: list[Path] = []
    seen: set[str] = set()
    for candidate in _dropped_path_candidates(text):
        for path in _expanded_existing_files(
            candidate,
            current_working_directory=Path(current_working_directory),
        ):
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths


def dropped_image_file_paths_from_text(
    text: str,
    *,
    current_working_directory: str | Path,
) -> list[Path]:
    return [
        path
        for path in dropped_file_paths_from_text(
            text,
            current_working_directory=current_working_directory,
        )
        if _is_image_path(path)
    ]


def _dropped_path_candidates(text: str) -> list[str]:
    stripped_text = text.strip()
    if stripped_text == "":
        return []
    if "\n" not in stripped_text and stripped_text.startswith("file://"):
        return [stripped_text]

    file_uri_candidates = _FILE_URI_RE.findall(stripped_text)
    if len(file_uri_candidates) > 0:
        return file_uri_candidates

    raw_candidate = [stripped_text] if "\n" not in stripped_text else []
    try:
        split_candidates = shlex.split(stripped_text)
    except ValueError:
        split_candidates = []
    if len(split_candidates) > 0:
        return raw_candidate + [
            candidate for candidate in split_candidates if candidate != stripped_text
        ]
    if len(raw_candidate) > 0:
        return raw_candidate

    return [line.strip() for line in stripped_text.splitlines() if line.strip() != ""]


def _expanded_existing_files(
    candidate: str,
    *,
    current_working_directory: Path,
) -> list[Path]:
    candidate_path = _path_from_candidate(candidate)
    if candidate_path is None:
        return []
    if not candidate_path.is_absolute():
        candidate_path = current_working_directory / candidate_path
    try:
        resolved = candidate_path.expanduser().resolve()
    except OSError:
        return []
    if resolved.is_file():
        return [resolved]
    if not resolved.is_dir():
        return []
    return sorted(
        (path.resolve() for path in resolved.rglob("*") if path.is_file()),
        key=str,
    )


def _path_from_candidate(candidate: str) -> Path | None:
    normalized = candidate.strip().strip("\"'").replace("\x00", "")
    if normalized == "":
        return None
    parsed = urlparse(normalized)
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            return None
        return Path(unquote(parsed.path))
    return Path(normalized)


def _is_image_path(path: Path) -> bool:
    mime_type = mimetypes.guess_type(str(path))[0]
    return isinstance(mime_type, str) and mime_type.startswith("image/")
