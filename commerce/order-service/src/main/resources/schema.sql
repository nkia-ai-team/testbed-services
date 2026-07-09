CREATE SCHEMA IF NOT EXISTS order_schema;

CREATE TABLE IF NOT EXISTS order_schema.orders (
    id              BIGSERIAL PRIMARY KEY,
    customer_name   VARCHAR(100) NOT NULL,
    customer_email  VARCHAR(200),
    total_amount    DECIMAL(12,2) NOT NULL DEFAULT 0,
    status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_schema.order_items (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT NOT NULL REFERENCES order_schema.orders(id),
    product_id  BIGINT NOT NULL,
    product_name VARCHAR(200),
    quantity    INT NOT NULL,
    unit_price  DECIMAL(12,2) NOT NULL,
    subtotal    DECIMAL(12,2) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_schema.order_items(order_id);
