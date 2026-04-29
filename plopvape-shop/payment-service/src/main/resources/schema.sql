CREATE SCHEMA IF NOT EXISTS payment_schema;

CREATE TABLE IF NOT EXISTS payment_schema.payments (
    id                BIGSERIAL PRIMARY KEY,
    order_id          BIGINT NOT NULL,
    amount            DECIMAL(12,2) NOT NULL,
    method            VARCHAR(30) NOT NULL DEFAULT 'CARD',
    status            VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    pg_transaction_id VARCHAR(100),
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payment_schema.payment_logs (
    id          BIGSERIAL PRIMARY KEY,
    payment_id  BIGINT NOT NULL REFERENCES payment_schema.payments(id),
    action      VARCHAR(50) NOT NULL,
    detail      TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payments_order ON payment_schema.payments(order_id);
CREATE INDEX IF NOT EXISTS idx_payment_logs_payment ON payment_schema.payment_logs(payment_id);
