CREATE SCHEMA IF NOT EXISTS shipping_schema;

CREATE TABLE IF NOT EXISTS shipping_schema.shipments (
    id                  BIGSERIAL PRIMARY KEY,
    order_id            BIGINT NOT NULL UNIQUE,
    status              VARCHAR(20) NOT NULL DEFAULT 'CREATED',
    tracking_number     VARCHAR(50),
    carrier             VARCHAR(50) NOT NULL DEFAULT 'CJ대한통운',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shipping_schema.shipment_events (
    id              BIGSERIAL PRIMARY KEY,
    shipment_id     BIGINT NOT NULL REFERENCES shipping_schema.shipments(id),
    status          VARCHAR(20) NOT NULL,
    occurred_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_shipment_events_shipment ON shipping_schema.shipment_events(shipment_id);
CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipping_schema.shipments(status);

CREATE TABLE IF NOT EXISTS shipping_schema.outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outbox_events_unpublished ON shipping_schema.outbox_events(created_at) WHERE published_at IS NULL;
