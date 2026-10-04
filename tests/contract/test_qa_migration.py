import os
import sqlite3
import subprocess
import sys

from app.config import ROOT


def test_upgrade_preserves_legacy_policy_and_downgrade_preserves_history(tmp_path):
    database = tmp_path / "migration.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def alembic(*arguments):
        result = subprocess.run([sys.executable, "-m", "alembic", "-c", "apps/api/alembic.ini", *arguments],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    alembic("upgrade", "0006")
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO conversations (id, owner_id, title, locale, epoch, request_revision, "
                   "event_seq, history, slots, summary, tool_config_version, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("old-call", "operator", "Existing call", "en-US", 0, 0, 0,
                    "[]", '{"product_model":"AX100"}', "Existing history", "1", "2026-10-03", "2026-10-03"))
    alembic("upgrade", "head")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT qa_execution_mode, answer_policy, qa_toolset_version FROM conversations").fetchone() == (
            "legacy", "knowledge_required", "legacy")
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0008",)
        assert db.execute("SELECT context_state FROM conversations").fetchone() == ("{}",)
        assert db.execute("SELECT slots FROM conversations").fetchone() == ('{"product_model":"AX100"}',)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"utterances", "delivery_attempts"} <= tables
    alembic("check")
    alembic("downgrade", "0007")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT summary FROM conversations").fetchone() == ("Existing history",)
        assert "context_state" not in {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
    alembic("upgrade", "head")
    alembic("downgrade", "0006")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT summary FROM conversations").fetchone() == ("Existing history",)
    alembic("upgrade", "head")
