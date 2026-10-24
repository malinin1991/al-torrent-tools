"""Content-versioned URLs keep browser caches in sync with rendered pages."""
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"


def static_asset_url(filename: str) -> str:
    relative = Path(filename)
    asset = (STATIC_ROOT / relative).resolve()
    if relative.is_absolute() or not asset.is_relative_to(STATIC_ROOT.resolve()):
        raise ValueError("Static asset must be inside the static directory")
    version = sha256(asset.read_bytes()).hexdigest()[:16]
    return f"/static/{quote(relative.as_posix(), safe='/')}?v={version}"
