CREATE SCHEMA IF NOT EXISTS inventory_schema;

CREATE TABLE IF NOT EXISTS inventory_schema.inventory (
    id          BIGSERIAL PRIMARY KEY,
    product_id  BIGINT NOT NULL UNIQUE,
    stock       INT NOT NULL DEFAULT 0,
    reserved    INT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 재고 변동 원장. reconciliation 배치가 inventory.stock과 대사하는 기준 로그.
CREATE TABLE IF NOT EXISTS inventory_schema.inventory_movements (
    id               BIGSERIAL PRIMARY KEY,
    product_id       BIGINT NOT NULL,
    movement_type    VARCHAR(20) NOT NULL,
    quantity         INT NOT NULL,
    resulting_stock  INT NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_inventory_movements_product ON inventory_schema.inventory_movements(product_id, created_at DESC);

CREATE TABLE IF NOT EXISTS inventory_schema.outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outbox_events_unpublished ON inventory_schema.outbox_events(created_at) WHERE published_at IS NULL;
