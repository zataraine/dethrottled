"""Where state lives.

One place, because four modules used to each resolve their own path relative to
__file__ and all four assumed the package sat inside a larger application tree.
An installed package has no such tree -- and writing cache files into
site-packages is wrong even when it happens to be writable.

Two directories, not one, because they have different lifecycles:

    data    caches, the corpus, engine and domain health. Disposable. Delete
            any of it and the system refills it.
    models  ONNX weights. Half a gigabyte, downloaded once, and emphatically
            not disposable.

They are kept independent on purpose. `model_dir()` does NOT follow
`DETHROTTLED_DATA_DIR`, because a caller pointing the data directory at a
scratch path -- which every test does, and which is a perfectly reasonable
thing to do for a one-off run -- would otherwise silently orphan the models and
trigger a 552MB re-download, or more likely just fail to find them.
"""
from __future__ import annotations

import os
from pathlib import Path


def _cache_root() -> Path:
    """The platform's cache location, ignoring any per-run overrides."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "dethrottled"


def data_dir() -> Path:
    """The directory holding the caches, the corpus and the health files.

    XDG on Linux and macOS, LOCALAPPDATA on Windows, and DETHROTTLED_DATA_DIR
    over both. Created on demand rather than at import: importing a library
    should not make directories.
    """
    override = os.environ.get("DETHROTTLED_DATA_DIR", "").strip()
    path = Path(override).expanduser() if override else _cache_root()
    path.mkdir(parents=True, exist_ok=True)
    return path


def model_dir() -> Path:
    """Where the ONNX weights are.

    Not created: an absent directory is the signal that the semantic features
    are simply not installed, and every caller already treats it that way.

    Deliberately independent of data_dir() -- see the module docstring.
    """
    override = os.environ.get("DETHROTTLED_MODEL_DIR", "").strip()
    return Path(override).expanduser() if override else _cache_root() / "models"
