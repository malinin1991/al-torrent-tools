from app.services.system_status import _APP_PACKAGES, _pkg_version


def test_pkg_versions_resolve() -> None:
    assert "fastapi" in _APP_PACKAGES
    assert _pkg_version("fastapi") not in {"", None}
    assert _pkg_version("definitely-missing-package-xyz") == "—"
