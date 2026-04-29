CREATE SCHEMA IF NOT EXISTS inventory_schema;

CREATE TABLE IF NOT EXISTS inventory_schema.inventory (
    id          BIGSERIAL PRIMARY KEY,
    product_id  BIGINT NOT NULL UNIQUE,
    stock       INT NOT NULL DEFAULT 0,
    reserved    INT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
