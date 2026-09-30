"""Tests for SQLAlchemy ORM model definitions."""
import uuid
from datetime import datetime, timezone
from backend.db.models import Base, Conversation, Message, UsageLog, User, Workspace

def test_user_model_has_required_columns():
    cols = {c.name for c in User.__table__.columns}
    assert cols == {"id", "email", "password_hash", "name", "is_active", "created_at", "updated_at"}

def test_workspace_model_has_required_columns():
    cols = {c.name for c in Workspace.__table__.columns}
    assert cols == {"id", "user_id", "name", "agent_type", "config", "memory", "is_deleted", "created_at", "updated_at"}

def test_conversation_model_has_required_columns():
    cols = {c.name for c in Conversation.__table__.columns}
    assert cols == {"id", "user_id", "workspace_id", "title", "tag", "is_deleted", "created_at", "updated_at"}

def test_message_model_has_required_columns():
    cols = {c.name for c in Message.__table__.columns}
    assert cols == {"id", "conversation_id", "role", "content", "data", "chart", "created_at"}

def test_usage_log_model_has_required_columns():
    cols = {c.name for c in UsageLog.__table__.columns}
    assert cols == {"id", "message_id", "user_id", "workspace_id", "conversation_id", "total_input_tokens", "total_output_tokens", "total_cost_usd", "model_primary", "latency_ms", "agent_calls", "created_at"}

def test_workspace_default_agent_type():
    ws = Workspace.__table__.columns["agent_type"]
    assert ws.default.arg == "tally"

def test_base_has_metadata():
    """One Base for the 9 app tables and the 21 sync tables (v2 merge M4); it was 9 before the merge."""
    from backend.db.sync_models import SYNC_TABLES

    assert Base.metadata is not None
    assert len(Base.metadata.tables) == 30
    assert len(set(Base.metadata.tables) - set(SYNC_TABLES)) == 9
