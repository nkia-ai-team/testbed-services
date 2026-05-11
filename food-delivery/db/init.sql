-- food-delivery testbed schema + seed
-- 6 tables matching architecture spec; seeded with 3 restaurants × 5 menus.

CREATE TABLE IF NOT EXISTS restaurants (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    region      VARCHAR(64),
    status      VARCHAR(16) DEFAULT 'OPEN'
);

CREATE TABLE IF NOT EXISTS menus (
    id            BIGSERIAL PRIMARY KEY,
    restaurant_id BIGINT NOT NULL REFERENCES restaurants(id),
    name          VARCHAR(128) NOT NULL,
    price         DECIMAL(10,2),
    available     BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS orders (
    id            BIGSERIAL PRIMARY KEY,
    customer_id   VARCHAR(64) NOT NULL,
    restaurant_id BIGINT NOT NULL REFERENCES restaurants(id),
    total_amount  DECIMAL(10,2) DEFAULT 0,
    status        VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT NOT NULL REFERENCES orders(id),
    menu_id     BIGINT NOT NULL REFERENCES menus(id),
    qty         INT NOT NULL,
    unit_price  DECIMAL(10,2) NOT NULL
);

-- dispatches / payments: order_id 에 FK constraint 두지 않음 (cross-service eventual consistency).
-- 이유: order-service @Transactional 안에서 fan-out (dispatch + payment) 시 order commit 전이라
--       FK 검증이 race 로 실패. microservice 경계에서 cross-table FK 는 정합성 부담만 큼.
-- 정합성: orphan dispatches/payments 는 외부 reconciliation 작업 (별도 job) 책임.
CREATE TABLE IF NOT EXISTS dispatches (
    id           BIGSERIAL PRIMARY KEY,
    order_id     BIGINT NOT NULL,
    courier_id   VARCHAR(64),
    eta_minutes  INT,
    status       VARCHAR(16) NOT NULL DEFAULT 'ASSIGNED',
    assigned_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payments (
    id            BIGSERIAL PRIMARY KEY,
    order_id      BIGINT NOT NULL,
    pg_provider   VARCHAR(32),
    amount        DECIMAL(10,2) NOT NULL,
    status        VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    processed_at  TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_menus_restaurant ON menus(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_orders_restaurant ON orders(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_dispatches_order ON dispatches(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id);

-- Seed: 3 restaurants
INSERT INTO restaurants (id, name, region, status) VALUES
    (1, '강남 떡볶이 본점', 'GANGNAM', 'OPEN'),
    (2, '홍대 마라탕', 'HONGDAE', 'OPEN'),
    (3, '잠실 치킨하우스', 'JAMSIL', 'OPEN')
ON CONFLICT (id) DO NOTHING;

SELECT setval('restaurants_id_seq', GREATEST((SELECT MAX(id) FROM restaurants), 1));

-- Seed: 5 menus per restaurant (15 total)
INSERT INTO menus (id, restaurant_id, name, price, available) VALUES
    (1, 1, '국물 떡볶이', 7000.00, TRUE),
    (2, 1, '로제 떡볶이', 9000.00, TRUE),
    (3, 1, '치즈 떡볶이', 8500.00, TRUE),
    (4, 1, '순대 한 접시', 5000.00, TRUE),
    (5, 1, '튀김 모듬', 6000.00, TRUE),
    (6, 2, '기본 마라탕', 12000.00, TRUE),
    (7, 2, '곱창 마라탕', 15000.00, TRUE),
    (8, 2, '해물 마라탕', 14000.00, TRUE),
    (9, 2, '마라샹궈', 18000.00, TRUE),
    (10, 2, '꿔바로우', 13000.00, TRUE),
    (11, 3, '후라이드 치킨', 18000.00, TRUE),
    (12, 3, '양념 치킨', 19000.00, TRUE),
    (13, 3, '간장 치킨', 19500.00, TRUE),
    (14, 3, '치킨 무 (대)', 1500.00, TRUE),
    (15, 3, '콜라 1.25L', 3000.00, TRUE)
ON CONFLICT (id) DO NOTHING;

SELECT setval('menus_id_seq', GREATEST((SELECT MAX(id) FROM menus), 1));
