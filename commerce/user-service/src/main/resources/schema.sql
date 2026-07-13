CREATE SCHEMA IF NOT EXISTS user_schema;

CREATE TABLE IF NOT EXISTS user_schema.users (
    id              BIGSERIAL PRIMARY KEY,
    email           VARCHAR(200) NOT NULL UNIQUE,
    password_hash   VARCHAR(100) NOT NULL,
    name            VARCHAR(100) NOT NULL,
    phone           VARCHAR(30),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_schema.addresses (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES user_schema.users(id),
    recipient_name  VARCHAR(100) NOT NULL,
    phone           VARCHAR(30),
    zipcode         VARCHAR(10),
    address1        VARCHAR(300) NOT NULL,
    address2        VARCHAR(200),
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_addresses_user ON user_schema.addresses(user_id);

-- 로그인 시 발급하는 opaque 토큰(UUID). 서명된 JWT까지는 가지 않고 단순 조회 검증으로 충분한 테스트베드 범위.
CREATE TABLE IF NOT EXISTS user_schema.auth_tokens (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES user_schema.users(id),
    token           VARCHAR(64) NOT NULL UNIQUE,
    expires_at      TIMESTAMP NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_token ON user_schema.auth_tokens(token);

CREATE TABLE IF NOT EXISTS user_schema.outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outbox_events_unpublished ON user_schema.outbox_events(created_at) WHERE published_at IS NULL;
