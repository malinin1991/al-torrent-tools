"""Router/render integration without importing app.main or external systems."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
import pytest

from app.api.hevc_workbench import create_hevc_workbench_router
from app.db.session import get_db
from test_hevc_workbench import db, listing, receipt, source


@pytest.fixture
def client(db):
    templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "app/templates"), context_processors=[lambda _: {"video_kensetsu_enabled": True}])
    application = FastAPI()
    application.include_router(create_hevc_workbench_router(templates))
    application.dependency_overrides[get_db] = lambda: db
    with TestClient(application) as browser:
        yield browser


def test_page_and_live_share_exact_data_and_escape_user_content(db, tmp_path, client):
    file, archive = source(db, tmp_path, relative="Show/<input>.mkv")
    archive.anime_name = "<script>alert(1)</script>"
    from app.db.models import Release
    db.get(Release, 42).title = archive.anime_name
    db.commit()
    full = client.get("/hevc-workbench")
    live = client.get("/hevc-workbench/live")
    assert full.status_code == live.status_code == 200
    for html in (full.text, live.text):
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "&lt;input&gt;.mkv" in html
        assert "<script>alert(1)</script>" not in html
        assert f'data-file-id="{file.id}"' in html and 'class="hevc-encode-check"' in html
        assert "по данным последней проверки БД" in html and "Приём Kensetsu" in html
    assert '/static/hevc_workbench.js' in full.text and 'data-ui-sse-channel="hevc_workbench"' in full.text
    assert "<html" not in live.text


def test_unknown_has_history_and_disabled_selection_with_no_retry_bypass(db, tmp_path, client):
    file, _ = source(db, tmp_path)
    group = receipt(db, file, "unknown")
    html = client.get("/hevc-workbench/live?view=unknown").text
    assert f"/encoder-history?submission_id={group.id}" in html
    assert "Приём не подтверждён" in html and "disabled" in html
    assert "encoder-history-retry" not in html and "allow_repeat" not in html and "acknowledge_unknown" not in html


def test_ready_failed_fs_is_explained_and_not_silently_discarded(db, tmp_path, client):
    file, _ = source(db, tmp_path)
    Path(file.full_path).unlink()
    html = client.get("/hevc-workbench/live?view=ready").text
    assert "Доступность изменилась при проверке" in html and "Исходник отсутствует на диске" in html
    assert "Предварительных кандидатов по БД: 1" in html
    assert 'data-hevc-live-url="/hevc-workbench/live?view=ready' in html
