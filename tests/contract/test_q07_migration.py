import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def migrate(path, *args):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT / "apps/api",
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "apps/api"),
            "DATABASE_URL": f"sqlite+aiosqlite:///{path}",
        },
        capture_output=True,
        text=True,
    )


def test_q07_migration_preserves_legacy_turn_and_refuses_direct_downgrade(tmp_path):
    path = tmp_path / "legacy.db"
    assert migrate(path, "upgrade", "0006").returncode == 0
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO conversations (id,owner_id,title,locale,epoch,event_seq,history,summary,tool_config_version,created_at,updated_at,slots,request_revision) VALUES ('cid','owner','title','en-US',0,0,'[]','','1',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'{}',1)"
        )
        db.execute(
            "INSERT INTO turns (id,conversation_id,epoch,idempotency_key,request_hash,user_text,channel,status,created_at,request_revision,delivery_status,output_suppressed) VALUES ('legacy','cid',0,'one','hash','question','text','answered',CURRENT_TIMESTAMP,1,'accepted',0)"
        )
    result = migrate(path, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT execution_mode,knowledge_result FROM turns WHERE id='legacy'"
        ).fetchone() == ("external", None)
        db.execute("UPDATE turns SET execution_mode='direct', knowledge_result='{}' WHERE id='legacy'")
    refused = migrate(path, "downgrade", "0006")
    assert refused.returncode != 0 and "Cannot downgrade Q07 with direct turns" in refused.stderr
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT execution_mode,knowledge_result FROM turns WHERE id='legacy'"
        ).fetchone() == ("direct", "{}")
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0007",)


def test_empty_q07_schema_can_downgrade_and_upgrade(tmp_path):
    path = tmp_path / "empty.db"
    assert migrate(path, "upgrade", "head").returncode == 0
    assert migrate(path, "downgrade", "0006").returncode == 0
    assert migrate(path, "upgrade", "head").returncode == 0
