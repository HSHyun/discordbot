from __future__ import annotations


def ensure_tables(conn) -> None:
    """필요한 테이블과 인덱스를 생성하고 기본값을 정비한다."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS source (
                id SERIAL PRIMARY KEY,
                code VARCHAR(50) NOT NULL UNIQUE,
                name VARCHAR(200) NOT NULL,
                url_pattern TEXT NOT NULL,
                parser VARCHAR(100) NOT NULL,
                fetch_interval_minutes INT DEFAULT 60,
                is_active BOOLEAN NOT NULL DEFAULT FALSE,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS item (
                id BIGSERIAL PRIMARY KEY,
                source_id INT NOT NULL REFERENCES source(id),
                external_id VARCHAR(200) NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                author TEXT,
                content TEXT,
                published_at TIMESTAMPTZ,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                metadata JSONB DEFAULT '{}'::jsonb
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS item_asset (
                id BIGSERIAL PRIMARY KEY,
                item_id BIGINT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
                asset_type VARCHAR(50) NOT NULL,
                url TEXT,
                local_path TEXT,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_item_source_external
            ON item (source_id, external_id);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_item_asset_item_id
            ON item_asset (item_id);
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS item_summary (
                id BIGSERIAL PRIMARY KEY,
                item_id BIGINT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
                model_name TEXT NOT NULL,
                summary_text TEXT NOT NULL,
                summary_title TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                meta JSONB NOT NULL DEFAULT '{}'::jsonb
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_item_summary_item_id
            ON item_summary (item_id);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_item_summary_created_at
            ON item_summary (created_at);
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_item_summary_item_model
            ON item_summary (item_id, model_name);
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS comment (
                id BIGSERIAL PRIMARY KEY,
                item_id BIGINT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
                external_id TEXT NOT NULL,
                author TEXT,
                content TEXT,
                created_at TIMESTAMPTZ,
                is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                parent_id BIGINT REFERENCES comment(id) ON DELETE CASCADE
            );
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_comment_item_external
            ON comment (item_id, external_id);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_comment_item_id
            ON comment (item_id);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_comment_parent_id
            ON comment (parent_id);
            """
        )
        cur.execute(
            """
            ALTER TABLE source
            ALTER COLUMN is_active SET DEFAULT FALSE
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crawl_run_log (
                id BIGSERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                queued_count INT NOT NULL,
                fetched_count INT,
                filtered_count INT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS digest_subscription (
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT,
                channel_id BIGINT NOT NULL UNIQUE,
                hours_window INT NOT NULL DEFAULT 6,
                interval_minutes INT NOT NULL DEFAULT 360,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                last_run_at TIMESTAMPTZ,
                next_run_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_digest_subscription_next_run
            ON digest_subscription (is_active, next_run_at);
            """
        )
    conn.commit()
