import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
FIXTURE = json.loads((ROOT / "tests/postgres-fixture.json").read_text())
RUN_ID = CONFIG["run_id"]
NETWORK = f"altt-regular-workbench-tests-{RUN_ID}"
PG = f"altt-regular-workbench-pg-{RUN_ID}"
NAME = f"altt-workbench-api-smoke-{RUN_ID}"
RECEIPT = {"created_utc": datetime.now(timezone.utc).isoformat(), "commands": []}


def run(argv, *, check=True):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    RECEIPT["commands"].append({"argv": argv, "exit_code": done.returncode,
                                "stdout": done.stdout, "stderr": done.stderr})
    if check and done.returncode:
        raise RuntimeError(f"Command failed: {argv[:3]}: {done.stderr[-1200:]}")
    return done


try:
    image = json.loads(run(["docker", "image", "inspect", CONFIG["runtime_tag"]]).stdout)[0]
    RECEIPT["image_id"] = image["Id"]
    # This database belongs to the disposable PostgreSQL only, separate from pytest.
    pg = json.loads(run(["docker", "inspect", PG]).stdout)[0]
    assert pg["Config"]["Labels"]["altt.regular-workbench"] == RUN_ID
    run(["docker", "exec", PG, "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", "CREATE DATABASE altt_workbench_smoke"])
    db_url = f"postgresql+psycopg://postgres@{PG}:5432/altt_workbench_smoke"
    container = run([
        "docker", "run", "--detach", "--name", NAME,
        "--label", f"altt.regular-workbench={RUN_ID}", "--network", NETWORK,
        "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/anilibria",
        "--env", f"DATABASE_URL={db_url}", "--env", "APP_ENV=prod",
        "--env", "SECRET_KEY=disposable-workbench-smoke-key",
        "--env", "TORRENT_STORAGE_DIR=/tmp/torrents",
        "--env", "ANILIBRIA_MEDIA_ROOT=/anilibria",
        "--env", "PYTHONDONTWRITEBYTECODE=1", image["Id"],
        "sh", "/app/scripts/entrypoint.sh", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", "8000", "--loop", "uvloop", "--http", "httptools",
    ]).stdout.strip()
    RECEIPT["container_id"] = container
    client = """
import hashlib,json,urllib.request
results=[]
for path in ['/health/live','/hevc-workbench','/hevc-workbench/live?view=all&page=1&per_page=30']:
    with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=5) as response:
        body=response.read()
        assert response.status==200
        if path=='/health/live': assert json.loads(body)=={'status':'ok'}
        else: assert 'HEVC' in body.decode('utf-8')
        results.append({'path':path,'status':response.status,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'content_type':response.headers['Content-Type']})
print(json.dumps(results))
"""
    for attempt in range(40):
        probe = run(["docker", "exec", NAME, "python", "-c", client], check=False)
        if probe.returncode == 0:
            RECEIPT["http"] = json.loads(probe.stdout)
            break
        time.sleep(1)
    else:
        raise RuntimeError("API failed to become ready within 40 probes")
    RECEIPT["migrations"] = run(["docker", "exec", PG, "psql", "-U", "postgres", "-d", "altt_workbench_smoke", "-Atc", "SELECT version_num FROM alembic_version"]).stdout.strip()
    RECEIPT["ok"] = True
except Exception as exc:
    RECEIPT["ok"] = False
    RECEIPT["error"] = repr(exc)
    raise
finally:
    logs = run(["docker", "logs", NAME], check=False)
    (ROOT / "logs/runtime-api-smoke.log").write_text(logs.stdout + logs.stderr)
    run(["docker", "stop", "--time", "10", NAME], check=False)
    run(["docker", "rm", NAME], check=False)
    (ROOT / "runtime-api-smoke.json").write_text(json.dumps(RECEIPT, indent=2) + "\n")
    print(json.dumps({key: RECEIPT.get(key) for key in ("ok", "image_id", "migrations", "http", "error")}, indent=2))
