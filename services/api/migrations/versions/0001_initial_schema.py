"""Initial schema.

Creates the full Ledgerline data model: identity, broker connections, instrument
reference data, the order/execution/trade write path, the journal, the strategy and
compliance catalog, computed analytics, AI analysis, and market data.

Three things in here are worth knowing before editing:

**Enum types are created explicitly, once, up front.** Several enums are shared by more
than one table (``order_side`` by orders and executions; ``timeframe`` by bars,
screenshots and replays). Letting ``create_table`` emit ``CREATE TYPE`` implicitly would
try to create each shared type more than once, so every column below declares
``create_type=False`` against a type this migration already made.

**TimescaleDB is optional.** ``market_bars`` and ``equity_curve_points`` become
hypertables when the extension is available and stay ordinary tables when it is not.
Local development and CI therefore run on stock Postgres with identical semantics —
only the partitioning differs — while production gets chunked storage and compression.

**Money is ``NUMERIC(20, 8)`` everywhere.** No floats, no ``money`` type.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Bar data is written in strict time order and read in windows, so a week per chunk
#: keeps the working set of a typical replay query inside one or two chunks.
BAR_CHUNK_INTERVAL = "7 days"
EQUITY_CHUNK_INTERVAL = "90 days"


def _timescale_available() -> bool:
    """Is the TimescaleDB extension installed and creatable on this server?"""
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
        ).scalar()
    )


def upgrade() -> None:
    # --- Enum types -------------------------------------------------------
    op.execute("CREATE TYPE account_type AS ENUM ('live', 'evaluation', 'funded', 'simulated')")
    op.execute("CREATE TYPE analysis_subject AS ENUM ('trade', 'day', 'week', 'month', 'quarter', 'year', 'strategy', 'setup')")
    op.execute("CREATE TYPE asset_class AS ENUM ('future', 'future_option', 'equity', 'equity_option', 'forex', 'crypto', 'cfd')")
    op.execute("CREATE TYPE broker_kind AS ENUM ('tradovate', 'manual', 'csv')")
    op.execute("CREATE TYPE connection_status AS ENUM ('pending', 'active', 'degraded', 'revoked', 'error')")
    op.execute("CREATE TYPE emotion AS ENUM ('calm', 'confident', 'anxious', 'fearful', 'greedy', 'frustrated', 'bored', 'revengeful', 'fomo')")
    op.execute("CREATE TYPE execution_role AS ENUM ('entry', 'exit')")
    op.execute("CREATE TYPE metric_scope AS ENUM ('overall', 'account', 'instrument', 'strategy', 'setup', 'market_condition', 'direction', 'hour_of_day', 'weekday', 'month', 'session_segment', 'duration_bucket', 'consecutive_wins', 'consecutive_losses', 'drawdown_state')")
    op.execute("CREATE TYPE order_side AS ENUM ('buy', 'sell')")
    op.execute("CREATE TYPE order_status AS ENUM ('pending', 'working', 'partially_filled', 'filled', 'cancelled', 'rejected', 'expired')")
    op.execute("CREATE TYPE order_type AS ENUM ('market', 'limit', 'stop', 'stop_limit', 'trailing_stop', 'market_if_touched', 'other')")
    op.execute("CREATE TYPE report_status AS ENUM ('queued', 'generating', 'ready', 'failed')")
    op.execute("CREATE TYPE report_type AS ENUM ('daily', 'weekly', 'monthly', 'quarterly', 'annual', 'compliance')")
    op.execute("CREATE TYPE rule_severity AS ENUM ('advisory', 'minor', 'major', 'critical')")
    op.execute("CREATE TYPE rule_type AS ENUM ('entry', 'exit', 'stop', 'target', 'filter', 'risk', 'management', 'checklist')")
    op.execute("CREATE TYPE screenshot_kind AS ENUM ('before_entry', 'entry', 'exit', 'after_exit', 'higher_timeframe', 'execution_timeframe')")
    op.execute("CREATE TYPE session_segment AS ENUM ('overnight', 'asia', 'london', 'pre_market', 'rth_open', 'rth_morning', 'rth_midday', 'rth_afternoon', 'rth_close', 'post_market')")
    op.execute("CREATE TYPE sync_status AS ENUM ('running', 'succeeded', 'failed', 'partial')")
    op.execute("CREATE TYPE time_in_force AS ENUM ('day', 'gtc', 'ioc', 'fok', 'gtd')")
    op.execute("CREATE TYPE timeframe AS ENUM ('1s', '5s', '15s', '30s', '1m', '2m', '3m', '5m', '10m', '15m', '30m', '1h', '2h', '4h', '1d', '1w')")
    op.execute("CREATE TYPE trade_direction AS ENUM ('long', 'short')")
    op.execute("CREATE TYPE trade_status AS ENUM ('open', 'closed')")

    # --- Tables and indexes -----------------------------------------------
    op.create_table('instruments',
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('root_symbol', sa.String(length=16), nullable=False),
    sa.Column('exchange', sa.String(length=32), nullable=False),
    sa.Column('description', sa.String(length=160), nullable=True),
    sa.Column('asset_class', postgresql.ENUM('future', 'future_option', 'equity', 'equity_option', 'forex', 'crypto', 'cfd', name='asset_class', create_type=False), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default='USD', nullable=False),
    sa.Column('tick_size', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('tick_value', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('price_precision', sa.Integer(), server_default='2', nullable=False),
    sa.Column('exchange_timezone', sa.String(length=64), server_default='America/Chicago', nullable=False),
    sa.Column('session_rollover', sa.String(length=8), server_default='17:00', nullable=False),
    sa.Column('expiry_date', sa.Date(), nullable=True),
    sa.Column('is_continuous', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_instruments')),
    sa.UniqueConstraint('symbol', 'exchange', name='uq_instruments_symbol_exchange')
    )
    op.create_index('ix_instruments_root_symbol', 'instruments', ['root_symbol'], unique=False)
    op.create_table('users',
    sa.Column('clerk_user_id', sa.String(length=128), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=120), nullable=True),
    sa.Column('timezone', sa.String(length=64), server_default='UTC', nullable=False),
    sa.Column('base_currency', sa.String(length=3), server_default='USD', nullable=False),
    sa.Column('onboarded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('clerk_user_id', name=op.f('uq_users_clerk_user_id'))
    )
    op.create_index(op.f('ix_users_deleted_at'), 'users', ['deleted_at'], unique=False)
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=False)
    op.create_table('ai_analyses',
    sa.Column('subject_type', postgresql.ENUM('trade', 'day', 'week', 'month', 'quarter', 'year', 'strategy', 'setup', name='analysis_subject', create_type=False), nullable=False),
    sa.Column('subject_id', sa.UUID(), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('prompt_version', sa.String(length=32), nullable=False),
    sa.Column('input_metrics', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('output', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('evidence_validated', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('confidence IS NULL OR (confidence >= 0 AND confidence <= 1)', name=op.f('ck_ai_analyses_confidence_range')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_ai_analyses_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_analyses'))
    )
    op.create_index('ix_ai_analyses_subject', 'ai_analyses', ['subject_type', 'subject_id'], unique=False)
    op.create_index('ix_ai_analyses_user_created', 'ai_analyses', ['user_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_ai_analyses_user_id'), 'ai_analyses', ['user_id'], unique=False)
    op.create_table('broker_connections',
    sa.Column('broker', postgresql.ENUM('tradovate', 'manual', 'csv', name='broker_kind', create_type=False), nullable=False),
    sa.Column('label', sa.String(length=80), nullable=False),
    sa.Column('environment', sa.String(length=16), server_default='demo', nullable=False),
    sa.Column('status', postgresql.ENUM('pending', 'active', 'degraded', 'revoked', 'error', name='connection_status', create_type=False), server_default='pending', nullable=False),
    sa.Column('credential_ref', sa.String(length=256), nullable=True),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('sync_cursor', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_broker_connections_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_broker_connections')),
    sa.UniqueConstraint('user_id', 'broker', 'label', name='uq_broker_connections_user_label')
    )
    op.create_index(op.f('ix_broker_connections_user_id'), 'broker_connections', ['user_id'], unique=False)
    op.create_table('daily_journals',
    sa.Column('journal_date', sa.Date(), nullable=False),
    sa.Column('pre_market_plan', sa.Text(), nullable=True),
    sa.Column('post_market_review', sa.Text(), nullable=True),
    sa.Column('market_notes', sa.Text(), nullable=True),
    sa.Column('mood', sa.String(length=32), nullable=True),
    sa.Column('sleep_hours', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('focus_score', sa.SmallInteger(), nullable=True),
    sa.Column('stress_score', sa.SmallInteger(), nullable=True),
    sa.Column('followed_plan', sa.Boolean(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('focus_score IS NULL OR (focus_score BETWEEN 1 AND 10)', name=op.f('ck_daily_journals_focus_score_range')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_daily_journals_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_daily_journals')),
    sa.UniqueConstraint('user_id', 'journal_date', name='uq_daily_journals_user_date')
    )
    op.create_index(op.f('ix_daily_journals_deleted_at'), 'daily_journals', ['deleted_at'], unique=False)
    op.create_index(op.f('ix_daily_journals_user_id'), 'daily_journals', ['user_id'], unique=False)
    op.create_table('detected_patterns',
    sa.Column('pattern_kind', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('polarity', sa.String(length=16), server_default='leak', nullable=False),
    sa.Column('sample_size', sa.Integer(), server_default='0', nullable=False),
    sa.Column('effect_size', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('p_value', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('confidence_low', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('confidence_high', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('is_significant', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('estimated_annual_impact', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('first_observed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_observed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('engine_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_detected_patterns_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_detected_patterns'))
    )
    op.create_index(op.f('ix_detected_patterns_user_id'), 'detected_patterns', ['user_id'], unique=False)
    op.create_index('ix_detected_patterns_user_kind', 'detected_patterns', ['user_id', 'pattern_kind'], unique=False)
    op.create_index('ix_detected_patterns_user_significant', 'detected_patterns', ['user_id', 'is_significant'], unique=False)
    op.create_table('market_bars',
    sa.Column('instrument_id', sa.UUID(), nullable=False),
    sa.Column('timeframe', postgresql.ENUM('1s', '5s', '15s', '30s', '1m', '2m', '3m', '5m', '10m', '15m', '30m', '1h', '2h', '4h', '1d', '1w', name='timeframe', create_type=False), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('open', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('high', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('low', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('close', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('volume', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('trade_count', sa.BigInteger(), nullable=True),
    sa.Column('source', sa.String(length=32), server_default='broker', nullable=False),
    sa.CheckConstraint('high >= low', name=op.f('ck_market_bars_high_at_least_low')),
    sa.CheckConstraint('high >= open AND high >= close', name=op.f('ck_market_bars_high_is_max')),
    sa.CheckConstraint('low <= open AND low <= close', name=op.f('ck_market_bars_low_is_min')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_market_bars_instrument_id_instruments'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('instrument_id', 'timeframe', 'ts', name=op.f('pk_market_bars'))
    )
    op.create_table('market_conditions',
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('is_system', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('criteria', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_market_conditions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_market_conditions')),
    sa.UniqueConstraint('user_id', 'name', name='uq_market_conditions_user_name')
    )
    op.create_index(op.f('ix_market_conditions_user_id'), 'market_conditions', ['user_id'], unique=False)
    op.create_table('notes',
    sa.Column('entity_type', sa.String(length=32), nullable=False),
    sa.Column('entity_id', sa.UUID(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('pinned', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_notes_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notes'))
    )
    op.create_index(op.f('ix_notes_deleted_at'), 'notes', ['deleted_at'], unique=False)
    op.create_index('ix_notes_entity', 'notes', ['entity_type', 'entity_id'], unique=False)
    op.create_index(op.f('ix_notes_user_id'), 'notes', ['user_id'], unique=False)
    op.create_table('setups',
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('is_system', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('detection_config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('color', sa.String(length=16), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_setups_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_setups')),
    sa.UniqueConstraint('user_id', 'name', name='uq_setups_user_name')
    )
    op.create_index(op.f('ix_setups_deleted_at'), 'setups', ['deleted_at'], unique=False)
    op.create_index(op.f('ix_setups_user_id'), 'setups', ['user_id'], unique=False)
    op.create_table('strategies',
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('supersedes_id', sa.UUID(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('effective_from', sa.DateTime(timezone=True), nullable=True),
    sa.Column('effective_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('checklist', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['supersedes_id'], ['strategies.id'], name=op.f('fk_strategies_supersedes_id_strategies'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_strategies_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_strategies')),
    sa.UniqueConstraint('user_id', 'name', 'version', name='uq_strategies_user_name_version')
    )
    op.create_index(op.f('ix_strategies_deleted_at'), 'strategies', ['deleted_at'], unique=False)
    op.create_index('ix_strategies_user_active', 'strategies', ['user_id', 'is_active'], unique=False)
    op.create_index(op.f('ix_strategies_user_id'), 'strategies', ['user_id'], unique=False)
    op.create_table('tags',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=True),
    sa.Column('color', sa.String(length=16), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_tags_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tags')),
    sa.UniqueConstraint('user_id', 'name', name='uq_tags_user_name')
    )
    op.create_index(op.f('ix_tags_user_id'), 'tags', ['user_id'], unique=False)
    op.create_table('user_settings',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('theme', sa.String(length=16), server_default='dark', nullable=False),
    sa.Column('default_risk_fraction', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('default_account_id', sa.UUID(), nullable=True),
    sa.Column('dashboard_layouts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('notification_preferences', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_settings_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_user_settings'))
    )
    op.create_table('accounts',
    sa.Column('broker_connection_id', sa.UUID(), nullable=True),
    sa.Column('broker_account_id', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('account_type', postgresql.ENUM('live', 'evaluation', 'funded', 'simulated', name='account_type', create_type=False), server_default='simulated', nullable=False),
    sa.Column('currency', sa.String(length=3), server_default='USD', nullable=False),
    sa.Column('prop_firm', sa.String(length=80), nullable=True),
    sa.Column('starting_balance', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('max_daily_loss', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('max_drawdown', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('profit_target', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['broker_connection_id'], ['broker_connections.id'], name=op.f('fk_accounts_broker_connection_id_broker_connections'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_accounts_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_accounts')),
    sa.UniqueConstraint('broker_connection_id', 'broker_account_id', name='uq_accounts_connection_broker_account')
    )
    op.create_index('ix_accounts_user_active', 'accounts', ['user_id', 'is_active'], unique=False)
    op.create_index(op.f('ix_accounts_user_id'), 'accounts', ['user_id'], unique=False)
    op.create_table('ai_recommendations',
    sa.Column('analysis_id', sa.UUID(), nullable=False),
    sa.Column('priority', sa.Integer(), server_default='3', nullable=False),
    sa.Column('category', sa.String(length=64), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('expected_improvement', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='open', nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('priority BETWEEN 1 AND 5', name=op.f('ck_ai_recommendations_priority_range')),
    sa.ForeignKeyConstraint(['analysis_id'], ['ai_analyses.id'], name=op.f('fk_ai_recommendations_analysis_id_ai_analyses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_ai_recommendations_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_recommendations'))
    )
    op.create_index(op.f('ix_ai_recommendations_user_id'), 'ai_recommendations', ['user_id'], unique=False)
    op.create_index('ix_ai_recommendations_user_status', 'ai_recommendations', ['user_id', 'status'], unique=False)
    op.create_table('strategy_rules',
    sa.Column('strategy_id', sa.UUID(), nullable=False),
    sa.Column('rule_type', postgresql.ENUM('entry', 'exit', 'stop', 'target', 'filter', 'risk', 'management', 'checklist', name='rule_type', create_type=False), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('severity', postgresql.ENUM('advisory', 'minor', 'major', 'critical', name='rule_severity', create_type=False), server_default='minor', nullable=False),
    sa.Column('weight', sa.Numeric(precision=18, scale=8), server_default='1', nullable=False),
    sa.Column('expression', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('weight >= 0', name=op.f('ck_strategy_rules_weight_non_negative')),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], name=op.f('fk_strategy_rules_strategy_id_strategies'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_strategy_rules_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_strategy_rules'))
    )
    op.create_index('ix_strategy_rules_strategy', 'strategy_rules', ['strategy_id'], unique=False)
    op.create_index(op.f('ix_strategy_rules_user_id'), 'strategy_rules', ['user_id'], unique=False)
    op.create_table('sync_runs',
    sa.Column('broker_connection_id', sa.UUID(), nullable=False),
    sa.Column('status', postgresql.ENUM('running', 'succeeded', 'failed', 'partial', name='sync_status', create_type=False), server_default='running', nullable=False),
    sa.Column('trigger', sa.String(length=32), server_default='scheduled', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('executions_ingested', sa.Integer(), server_default='0', nullable=False),
    sa.Column('executions_skipped', sa.Integer(), server_default='0', nullable=False),
    sa.Column('trades_written', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['broker_connection_id'], ['broker_connections.id'], name=op.f('fk_sync_runs_broker_connection_id_broker_connections'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_sync_runs_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sync_runs'))
    )
    op.create_index('ix_sync_runs_connection_started', 'sync_runs', ['broker_connection_id', 'started_at'], unique=False)
    op.create_index(op.f('ix_sync_runs_user_id'), 'sync_runs', ['user_id'], unique=False)
    op.create_table('account_balance_snapshots',
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('cash_balance', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('equity', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('open_pnl', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('realized_pnl_day', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('margin_used', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_account_balance_snapshots_account_id_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_account_balance_snapshots_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_account_balance_snapshots')),
    sa.UniqueConstraint('account_id', 'as_of', name='uq_account_balance_snapshots_account_as_of')
    )
    op.create_index('ix_account_balance_snapshots_account_ts', 'account_balance_snapshots', ['account_id', 'as_of'], unique=False)
    op.create_index(op.f('ix_account_balance_snapshots_user_id'), 'account_balance_snapshots', ['user_id'], unique=False)
    op.create_table('equity_curve_points',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('trade_id', sa.UUID(), nullable=True),
    sa.Column('cumulative_net_pnl', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('cumulative_r', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('peak_equity', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('drawdown', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('drawdown_pct', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('trade_number', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_equity_curve_points_account_id_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_equity_curve_points_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'account_id', 'recorded_at', name=op.f('pk_equity_curve_points'))
    )
    op.create_index('ix_equity_curve_points_user_time', 'equity_curve_points', ['user_id', 'recorded_at'], unique=False)
    op.create_table('orders',
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('instrument_id', sa.UUID(), nullable=False),
    sa.Column('broker_order_id', sa.String(length=128), nullable=False),
    sa.Column('parent_order_id', sa.UUID(), nullable=True),
    sa.Column('side', postgresql.ENUM('buy', 'sell', name='order_side', create_type=False), nullable=False),
    sa.Column('order_type', postgresql.ENUM('market', 'limit', 'stop', 'stop_limit', 'trailing_stop', 'market_if_touched', 'other', name='order_type', create_type=False), nullable=False),
    sa.Column('time_in_force', postgresql.ENUM('day', 'gtc', 'ioc', 'fok', 'gtd', name='time_in_force', create_type=False), nullable=True),
    sa.Column('status', postgresql.ENUM('pending', 'working', 'partially_filled', 'filled', 'cancelled', 'rejected', 'expired', name='order_status', create_type=False), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('filled_quantity', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('limit_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('stop_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('placed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('filled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_orders_account_id_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_orders_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['parent_order_id'], ['orders.id'], name=op.f('fk_orders_parent_order_id_orders'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_orders_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orders')),
    sa.UniqueConstraint('account_id', 'broker_order_id', name='uq_orders_account_broker_order')
    )
    op.create_index('ix_orders_account_instrument_placed', 'orders', ['account_id', 'instrument_id', 'placed_at'], unique=False)
    op.create_index(op.f('ix_orders_user_id'), 'orders', ['user_id'], unique=False)
    op.create_index('ix_orders_user_placed_at', 'orders', ['user_id', 'placed_at'], unique=False)
    op.create_table('performance_metrics',
    sa.Column('account_id', sa.UUID(), nullable=True),
    sa.Column('scope', postgresql.ENUM('overall', 'account', 'instrument', 'strategy', 'setup', 'market_condition', 'direction', 'hour_of_day', 'weekday', 'month', 'session_segment', 'duration_bucket', 'consecutive_wins', 'consecutive_losses', 'drawdown_state', name='metric_scope', create_type=False), nullable=False),
    sa.Column('scope_key', sa.String(length=120), server_default='', nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('sample_size', sa.Integer(), server_default='0', nullable=False),
    sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('net_pnl', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('expectancy', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('expectancy_r', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('win_rate', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('profit_factor', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('engine_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('sample_size >= 0', name=op.f('ck_performance_metrics_sample_size_non_negative')),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_performance_metrics_account_id_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_performance_metrics_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_performance_metrics')),
    sa.UniqueConstraint('user_id', 'account_id', 'scope', 'scope_key', 'period_start', 'period_end', 'engine_version', name='uq_performance_metrics_slice')
    )
    op.create_index(op.f('ix_performance_metrics_user_id'), 'performance_metrics', ['user_id'], unique=False)
    op.create_index('ix_performance_metrics_user_scope', 'performance_metrics', ['user_id', 'scope', 'scope_key'], unique=False)
    op.create_table('reports',
    sa.Column('account_id', sa.UUID(), nullable=True),
    sa.Column('report_type', postgresql.ENUM('daily', 'weekly', 'monthly', 'quarterly', 'annual', 'compliance', name='report_type', create_type=False), nullable=False),
    sa.Column('status', postgresql.ENUM('queued', 'generating', 'ready', 'failed', name='report_status', create_type=False), server_default='queued', nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('metrics_snapshot', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('findings', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('ai_analysis_id', sa.UUID(), nullable=True),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('period_end >= period_start', name=op.f('ck_reports_period_ordered')),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_reports_account_id_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_reports_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reports')),
    sa.UniqueConstraint('user_id', 'account_id', 'report_type', 'period_start', name='uq_reports_user_type_period')
    )
    op.create_index(op.f('ix_reports_user_id'), 'reports', ['user_id'], unique=False)
    op.create_index('ix_reports_user_type_period', 'reports', ['user_id', 'report_type', 'period_start'], unique=False)
    op.create_table('risk_metrics',
    sa.Column('account_id', sa.UUID(), nullable=True),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sample_size', sa.Integer(), server_default='0', nullable=False),
    sa.Column('kelly_fraction', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('half_kelly_fraction', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('risk_of_ruin', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('ruin_threshold_pct', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('max_drawdown', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('max_drawdown_pct', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('average_drawdown', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('recovery_factor', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('mar_ratio', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('value_at_risk_95', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('conditional_var_95', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('simulation', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('assumptions', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('engine_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_risk_metrics_account_id_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_risk_metrics_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_metrics')),
    sa.UniqueConstraint('user_id', 'account_id', 'as_of', name='uq_risk_metrics_user_account_as_of')
    )
    op.create_index(op.f('ix_risk_metrics_user_id'), 'risk_metrics', ['user_id'], unique=False)
    op.create_table('trades',
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('instrument_id', sa.UUID(), nullable=False),
    sa.Column('origin_execution_id', sa.String(length=128), nullable=False),
    sa.Column('direction', postgresql.ENUM('long', 'short', name='trade_direction', create_type=False), nullable=False),
    sa.Column('status', postgresql.ENUM('open', 'closed', name='trade_status', create_type=False), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_seconds', sa.Integer(), nullable=True),
    sa.Column('quantity_opened', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('quantity_closed', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('max_position_size', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('avg_entry_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('avg_exit_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('realized_points', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('points_per_contract', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('ticks_per_contract', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('gross_pnl', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('commission', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('fees', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('net_pnl', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('scaled_in', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('scaled_out', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('session_date', sa.Date(), nullable=True),
    sa.Column('session_segment', postgresql.ENUM('overnight', 'asia', 'london', 'pre_market', 'rth_open', 'rth_morning', 'rth_midday', 'rth_afternoon', 'rth_close', 'post_market', name='session_segment', create_type=False), nullable=True),
    sa.Column('entry_hour', sa.SmallInteger(), nullable=True),
    sa.Column('entry_weekday', sa.SmallInteger(), nullable=True),
    sa.Column('initial_stop_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('initial_target_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('final_stop_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('planned_risk_amount', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('planned_reward_amount', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('planned_r_multiple', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('realized_r', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('mae_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('mfe_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('mae_amount', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('mfe_amount', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('mae_r', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('mfe_r', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('edge_ratio', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('strategy_id', sa.UUID(), nullable=True),
    sa.Column('setup_id', sa.UUID(), nullable=True),
    sa.Column('market_condition_id', sa.UUID(), nullable=True),
    sa.Column('setup_source', sa.String(length=16), nullable=True),
    sa.Column('setup_confidence', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('confidence', sa.SmallInteger(), nullable=True),
    sa.Column('emotion', postgresql.ENUM('calm', 'confident', 'anxious', 'fearful', 'greedy', 'frustrated', 'bored', 'revengeful', 'fomo', name='emotion', create_type=False), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('execution_quality_score', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('discipline_score', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('compliance_score', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('ai_summary', sa.Text(), nullable=True),
    sa.Column('ai_analysis_id', sa.UUID(), nullable=True),
    sa.Column('reconstruction_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'closed' AND closed_at IS NOT NULL) OR (status = 'open' AND closed_at IS NULL)", name=op.f('ck_trades_status_matches_closed_at')),
    sa.CheckConstraint('closed_at IS NULL OR closed_at >= opened_at', name=op.f('ck_trades_closed_after_opened')),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_trades_account_id_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_trades_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['market_condition_id'], ['market_conditions.id'], name=op.f('fk_trades_market_condition_id_market_conditions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['setup_id'], ['setups.id'], name=op.f('fk_trades_setup_id_setups'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], name=op.f('fk_trades_strategy_id_strategies'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_trades_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trades')),
    sa.UniqueConstraint('account_id', 'origin_execution_id', name='uq_trades_account_origin_execution')
    )
    op.create_index('ix_trades_account_instrument_opened', 'trades', ['account_id', 'instrument_id', 'opened_at'], unique=False)
    op.create_index(op.f('ix_trades_user_id'), 'trades', ['user_id'], unique=False)
    op.create_index('ix_trades_user_opened_at', 'trades', ['user_id', 'opened_at'], unique=False)
    op.create_index('ix_trades_user_session_date', 'trades', ['user_id', 'session_date'], unique=False)
    op.create_index('ix_trades_user_setup', 'trades', ['user_id', 'setup_id'], unique=False)
    op.create_index('ix_trades_user_status', 'trades', ['user_id', 'status'], unique=False)
    op.create_index('ix_trades_user_strategy', 'trades', ['user_id', 'strategy_id'], unique=False)
    op.create_table('trading_sessions',
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('session_date', sa.Date(), nullable=False),
    sa.Column('first_trade_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_trade_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('trade_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('win_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('loss_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('scratch_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('gross_pnl', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('commission', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('fees', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('net_pnl', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('total_r', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('max_intraday_drawdown', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('volume_contracts', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_trading_sessions_account_id_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_trading_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trading_sessions')),
    sa.UniqueConstraint('account_id', 'session_date', name='uq_trading_sessions_account_date')
    )
    op.create_index('ix_trading_sessions_user_date', 'trading_sessions', ['user_id', 'session_date'], unique=False)
    op.create_index(op.f('ix_trading_sessions_user_id'), 'trading_sessions', ['user_id'], unique=False)
    op.create_table('executions',
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('instrument_id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=True),
    sa.Column('broker_execution_id', sa.String(length=128), nullable=False),
    sa.Column('side', postgresql.ENUM('buy', 'sell', name='order_side', create_type=False), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('price', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sequence', sa.Integer(), server_default='0', nullable=False),
    sa.Column('commission', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('fees', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('liquidity_flag', sa.String(length=16), nullable=True),
    sa.Column('sync_run_id', sa.UUID(), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.CheckConstraint('price >= 0', name=op.f('ck_executions_price_non_negative')),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_executions_quantity_positive')),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_executions_account_id_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_executions_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_executions_order_id_orders'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['sync_run_id'], ['sync_runs.id'], name=op.f('fk_executions_sync_run_id_sync_runs'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_executions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_executions')),
    sa.UniqueConstraint('account_id', 'broker_execution_id', name='uq_executions_account_broker_execution')
    )
    op.create_index('ix_executions_account_instrument_executed', 'executions', ['account_id', 'instrument_id', 'executed_at'], unique=False)
    op.create_index('ix_executions_user_executed_at', 'executions', ['user_id', 'executed_at'], unique=False)
    op.create_index(op.f('ix_executions_user_id'), 'executions', ['user_id'], unique=False)
    op.create_table('positions',
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('instrument_id', sa.UUID(), nullable=False),
    sa.Column('signed_quantity', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('avg_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('open_trade_id', sa.UUID(), nullable=True),
    sa.Column('open_lots', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('last_execution_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_positions_account_id_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_positions_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['open_trade_id'], ['trades.id'], name=op.f('fk_positions_open_trade_id_trades'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_positions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_positions')),
    sa.UniqueConstraint('account_id', 'instrument_id', name='uq_positions_account_instrument')
    )
    op.create_index(op.f('ix_positions_user_id'), 'positions', ['user_id'], unique=False)
    op.create_table('replay_metadata',
    sa.Column('trade_id', sa.UUID(), nullable=False),
    sa.Column('instrument_id', sa.UUID(), nullable=False),
    sa.Column('primary_timeframe', postgresql.ENUM('1s', '5s', '15s', '30s', '1m', '2m', '3m', '5m', '10m', '15m', '30m', '1h', '2h', '4h', '1d', '1w', name='timeframe', create_type=False), nullable=False),
    sa.Column('higher_timeframe', postgresql.ENUM('1s', '5s', '15s', '30s', '1m', '2m', '3m', '5m', '10m', '15m', '30m', '1h', '2h', '4h', '1d', '1w', name='timeframe', create_type=False), nullable=True),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('context_bars_before', sa.Integer(), server_default='120', nullable=False),
    sa.Column('context_bars_after', sa.Integer(), server_default='60', nullable=False),
    sa.Column('markers', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('indicators', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('drawings', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_replay_metadata_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['trade_id'], ['trades.id'], name=op.f('fk_replay_metadata_trade_id_trades'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_replay_metadata_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_replay_metadata')),
    sa.UniqueConstraint('trade_id', 'primary_timeframe', name='uq_replay_metadata_trade_tf')
    )
    op.create_index('ix_replay_metadata_user', 'replay_metadata', ['user_id'], unique=False)
    op.create_index(op.f('ix_replay_metadata_user_id'), 'replay_metadata', ['user_id'], unique=False)
    op.create_table('rule_evaluations',
    sa.Column('trade_id', sa.UUID(), nullable=False),
    sa.Column('rule_id', sa.UUID(), nullable=False),
    sa.Column('passed', sa.Boolean(), nullable=False),
    sa.Column('evaluable', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('evaluated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['rule_id'], ['strategy_rules.id'], name=op.f('fk_rule_evaluations_rule_id_strategy_rules'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['trade_id'], ['trades.id'], name=op.f('fk_rule_evaluations_trade_id_trades'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_rule_evaluations_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rule_evaluations')),
    sa.UniqueConstraint('trade_id', 'rule_id', name='uq_rule_evaluations_trade_rule')
    )
    op.create_index(op.f('ix_rule_evaluations_user_id'), 'rule_evaluations', ['user_id'], unique=False)
    op.create_index('ix_rule_evaluations_user_rule_passed', 'rule_evaluations', ['user_id', 'rule_id', 'passed'], unique=False)
    op.create_table('screenshots',
    sa.Column('trade_id', sa.UUID(), nullable=False),
    sa.Column('kind', postgresql.ENUM('before_entry', 'entry', 'exit', 'after_exit', 'higher_timeframe', 'execution_timeframe', name='screenshot_kind', create_type=False), nullable=False),
    sa.Column('timeframe', postgresql.ENUM('1s', '5s', '15s', '30s', '1m', '2m', '3m', '5m', '10m', '15m', '30m', '1h', '2h', '4h', '1d', '1w', name='timeframe', create_type=False), nullable=True),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('content_type', sa.String(length=64), server_default='image/webp', nullable=False),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('byte_size', sa.Integer(), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('source', sa.String(length=16), server_default='auto', nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['trade_id'], ['trades.id'], name=op.f('fk_screenshots_trade_id_trades'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_screenshots_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_screenshots')),
    sa.UniqueConstraint('trade_id', 'kind', 'timeframe', name='uq_screenshots_trade_kind_tf')
    )
    op.create_index('ix_screenshots_user_captured', 'screenshots', ['user_id', 'captured_at'], unique=False)
    op.create_index(op.f('ix_screenshots_user_id'), 'screenshots', ['user_id'], unique=False)
    op.create_table('trade_tags',
    sa.Column('trade_id', sa.UUID(), nullable=False),
    sa.Column('tag_id', sa.UUID(), nullable=False),
    sa.Column('source', sa.String(length=16), server_default='manual', nullable=False),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], name=op.f('fk_trade_tags_tag_id_tags'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['trade_id'], ['trades.id'], name=op.f('fk_trade_tags_trade_id_trades'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('trade_id', 'tag_id', name=op.f('pk_trade_tags'))
    )
    op.create_index('ix_trade_tags_tag', 'trade_tags', ['tag_id'], unique=False)
    op.create_table('trade_executions',
    sa.Column('trade_id', sa.UUID(), nullable=False),
    sa.Column('execution_id', sa.UUID(), nullable=False),
    sa.Column('role', postgresql.ENUM('entry', 'exit', name='execution_role', create_type=False), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('price', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('allocated_commission', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('allocated_fees', sa.Numeric(precision=20, scale=8), server_default='0', nullable=False),
    sa.Column('realized_points', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('leg_index', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_trade_executions_quantity_positive')),
    sa.ForeignKeyConstraint(['execution_id'], ['executions.id'], name=op.f('fk_trade_executions_execution_id_executions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['trade_id'], ['trades.id'], name=op.f('fk_trade_executions_trade_id_trades'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('trade_id', 'execution_id', 'role', name=op.f('pk_trade_executions'))
    )
    op.create_index('ix_trade_executions_execution', 'trade_executions', ['execution_id'], unique=False)

    # --- TimescaleDB hypertables ------------------------------------------
    if _timescale_available():
        op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")
        op.execute(
            sa.text(
                "SELECT create_hypertable('market_bars', 'ts', "
                f"chunk_time_interval => INTERVAL '{BAR_CHUNK_INTERVAL}', "
                "migrate_data => TRUE, if_not_exists => TRUE)"
            )
        )
        op.execute(
            sa.text(
                "SELECT create_hypertable('equity_curve_points', 'recorded_at', "
                f"chunk_time_interval => INTERVAL '{EQUITY_CHUNK_INTERVAL}', "
                "migrate_data => TRUE, if_not_exists => TRUE)"
            )
        )
        # Bars older than 30 days are read-only history; compressing them typically
        # cuts storage by an order of magnitude with no change to query semantics.
        op.execute(
            "ALTER TABLE market_bars SET ("
            "timescaledb.compress, "
            "timescaledb.compress_segmentby = 'instrument_id, timeframe', "
            "timescaledb.compress_orderby = 'ts DESC')"
        )
        op.execute("SELECT add_compression_policy('market_bars', INTERVAL '30 days')")


def downgrade() -> None:
    op.drop_table("trade_executions")
    op.drop_table("trade_tags")
    op.drop_table("screenshots")
    op.drop_table("rule_evaluations")
    op.drop_table("replay_metadata")
    op.drop_table("positions")
    op.drop_table("executions")
    op.drop_table("trading_sessions")
    op.drop_table("trades")
    op.drop_table("risk_metrics")
    op.drop_table("reports")
    op.drop_table("performance_metrics")
    op.drop_table("orders")
    op.drop_table("equity_curve_points")
    op.drop_table("account_balance_snapshots")
    op.drop_table("sync_runs")
    op.drop_table("strategy_rules")
    op.drop_table("ai_recommendations")
    op.drop_table("accounts")
    op.drop_table("user_settings")
    op.drop_table("tags")
    op.drop_table("strategies")
    op.drop_table("setups")
    op.drop_table("notes")
    op.drop_table("market_conditions")
    op.drop_table("market_bars")
    op.drop_table("detected_patterns")
    op.drop_table("daily_journals")
    op.drop_table("broker_connections")
    op.drop_table("ai_analyses")
    op.drop_table("users")
    op.drop_table("instruments")

    op.execute("DROP TYPE IF EXISTS trade_status")
    op.execute("DROP TYPE IF EXISTS trade_direction")
    op.execute("DROP TYPE IF EXISTS timeframe")
    op.execute("DROP TYPE IF EXISTS time_in_force")
    op.execute("DROP TYPE IF EXISTS sync_status")
    op.execute("DROP TYPE IF EXISTS session_segment")
    op.execute("DROP TYPE IF EXISTS screenshot_kind")
    op.execute("DROP TYPE IF EXISTS rule_type")
    op.execute("DROP TYPE IF EXISTS rule_severity")
    op.execute("DROP TYPE IF EXISTS report_type")
    op.execute("DROP TYPE IF EXISTS report_status")
    op.execute("DROP TYPE IF EXISTS order_type")
    op.execute("DROP TYPE IF EXISTS order_status")
    op.execute("DROP TYPE IF EXISTS order_side")
    op.execute("DROP TYPE IF EXISTS metric_scope")
    op.execute("DROP TYPE IF EXISTS execution_role")
    op.execute("DROP TYPE IF EXISTS emotion")
    op.execute("DROP TYPE IF EXISTS connection_status")
    op.execute("DROP TYPE IF EXISTS broker_kind")
    op.execute("DROP TYPE IF EXISTS asset_class")
    op.execute("DROP TYPE IF EXISTS analysis_subject")
    op.execute("DROP TYPE IF EXISTS account_type")
