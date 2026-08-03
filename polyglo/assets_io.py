"""Read generated asset bytes back off disk.

NVIDIA providers write output to a local directory and return an ``Asset`` whose
``url`` is a ``file://`` URI pointing at it (confirmed by introspection — see
``docs/06``). This is the one place that URI gets turned into bytes, so every caller
(narrate.py, visuals.py) shares one implementation rather than each re-parsing it
slightly differently.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from genblaze_core.models import Asset

__all__ = ["AssetIOError", "read_asset_bytes"]


class AssetIOError(RuntimeError):
    pass


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    # file:///C:/foo on Windows parses with a leading slash before the drive letter.
    if path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return Path(path)


def read_asset_bytes(asset: Asset) -> bytes:
    """Fetch the bytes an ``Asset`` points to.

    Only ``file://`` is implemented — the only scheme NVIDIA's providers have been
    observed to produce with ``output_dir`` set. HTTP(S) asset URLs would need a
    fetch-and-hash step of their own; not needed until a provider that returns them
    is actually wired in, so raising clearly here beats a silent wrong answer.
    """
    scheme = urlparse(asset.url).scheme
    if scheme == "file":
        path = _file_uri_to_path(asset.url)
        if not path.is_file():
            raise AssetIOError(f"asset file not found: {path}")
        return path.read_bytes()
    raise AssetIOError(
        f"unsupported asset URL scheme {scheme!r} for {asset.url!r} — "
        f"only file:// is implemented"
    )
