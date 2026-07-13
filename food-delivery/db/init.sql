-- ============================================================
-- food-delivery 통합 스키마 + 시드 스크립트 (docker-entrypoint-initdb.d / K8s mysql-init-scripts 공용)
-- ============================================================
-- §5(스키마 확장) — 기존 6테이블(restaurants/menus/orders/order_items/dispatches/payments)에
-- outbox 3종 + 감사/집계 3종(dispatch_events는 7번에서 이미 생성) + 신규 도메인 3종
-- (menu_categories/customers/riders)을 더해 15테이블로 확장.
--
-- 재현성: MySQL은 setseed() 가 없다 — 스크립트 시작 시 RAND(42)를 한 번 호출해 세션의
-- 난수 스트림을 시딩하고, 이후 모든 RAND()는 인자 없이 호출해 그 스트림을 이어 쓴다
-- (docker-entrypoint-initdb.d/kubectl exec 로 이 파일 전체가 한 mysql 세션에서 실행되는
-- 것을 전제). commerce(PostgreSQL)와 마찬가지로 완전한 바이트 단위 재현이 목적이 아니라
-- 분포 특성 재현이 목적이다.
--
-- LATERAL/파생테이블 캐싱 함정 회피: commerce 대량시드 작업 중 "바깥 행을 참조하지 않는
-- LATERAL 서브쿼리는 플래너가 1회만 평가해 전체 행에 재사용해버린다"는 실측 버그를 만났다
-- (PostgreSQL 한정 이슈로 보이나, 동일 클래스의 실수를 반복하지 않기 위해 이 스크립트는
-- 파생테이블/서브쿼리 안에 RAND()를 가두지 않고, 재귀 CTE를 임시테이블로 물리화한 뒤 그
-- SELECT 목록에 직접 RAND() 호출을 늘어놓는 방식만 쓴다 — 파생테이블로 감싸는 지점이
-- 아예 없으므로 이 클래스의 버그가 발생할 지점 자체가 없다).

SELECT RAND(42);

-- 재귀 CTE 기본 한도(1000)로는 20,000행 시퀀스 생성이 중간에 끊긴다.
SET SESSION cte_max_recursion_depth = 100000;

-- ------------------------------------------------------------
-- 스키마 (15 테이블)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS restaurants (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    region      VARCHAR(64),
    status      VARCHAR(16) DEFAULT 'OPEN'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS menu_categories (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS menus (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    restaurant_id BIGINT NOT NULL,
    category_id   BIGINT,
    name          VARCHAR(128) NOT NULL,
    price         DECIMAL(10,2),
    available     BOOLEAN DEFAULT TRUE,
    KEY idx_menus_restaurant (restaurant_id),
    KEY idx_menus_category (category_id),
    CONSTRAINT fk_menus_restaurant FOREIGN KEY (restaurant_id) REFERENCES restaurants(id),
    CONSTRAINT fk_menus_category FOREIGN KEY (category_id) REFERENCES menu_categories(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 다른 서비스 소유 테이블(orders 등)과의 관계와 마찬가지로 FK 없이 느슨하게 참조한다
-- (orders.customer_id 는 'cust-<id>' 문자열로 customers.id 를 가리킬 뿐, cross-service
-- eventual consistency 원칙을 도메인 내부에도 동일하게 적용).
CREATE TABLE IF NOT EXISTS customers (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(64) NOT NULL,
    phone       VARCHAR(32),
    email       VARCHAR(128),
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS riders (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    name         VARCHAR(64) NOT NULL,
    phone        VARCHAR(32),
    vehicle_type VARCHAR(16) DEFAULT 'BIKE',
    status       VARCHAR(16) DEFAULT 'ACTIVE',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS orders (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id   VARCHAR(64) NOT NULL,
    restaurant_id BIGINT NOT NULL,
    total_amount  DECIMAL(10,2) DEFAULT 0,
    status        VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_orders_restaurant (restaurant_id),
    KEY idx_orders_customer (customer_id),
    KEY idx_orders_created_at (created_at),
    CONSTRAINT fk_orders_restaurant FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS order_items (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id    BIGINT NOT NULL,
    menu_id     BIGINT NOT NULL,
    qty         INT NOT NULL,
    unit_price  DECIMAL(10,2) NOT NULL,
    KEY idx_order_items_order (order_id),
    CONSTRAINT fk_order_items_order FOREIGN KEY (order_id) REFERENCES orders(id),
    CONSTRAINT fk_order_items_menu FOREIGN KEY (menu_id) REFERENCES menus(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS order_outbox_events (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP NULL,
    KEY idx_order_outbox_unpublished (published_at, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- dispatches / payments: order_id 에 FK 두지 않음 (cross-service eventual consistency).
CREATE TABLE IF NOT EXISTS dispatches (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id     BIGINT NOT NULL,
    courier_id   VARCHAR(64),
    eta_minutes  INT,
    status       VARCHAR(16) NOT NULL DEFAULT 'ASSIGNED',
    assigned_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_dispatches_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- (7번 이식) 배차 상태 전이 이력 — DispatchService.recordEvent() 가 기록.
CREATE TABLE IF NOT EXISTS dispatch_events (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    dispatch_id  BIGINT NOT NULL,
    status       VARCHAR(16) NOT NULL,
    occurred_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_dispatch_events_dispatch (dispatch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dispatch_outbox_events (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP NULL,
    KEY idx_dispatch_outbox_unpublished (published_at, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS payments (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id      BIGINT NOT NULL,
    pg_provider   VARCHAR(32),
    amount        DECIMAL(10,2) NOT NULL,
    status        VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    processed_at  TIMESTAMP NULL,
    settled_at    TIMESTAMP NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_payments_order (order_id),
    KEY idx_payments_settlement (status, settled_at, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS payment_outbox_events (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    aggregate_type  VARCHAR(50) NOT NULL,
    aggregate_id    VARCHAR(50) NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    topic           VARCHAR(100) NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at    TIMESTAMP NULL,
    KEY idx_payment_outbox_unpublished (published_at, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- (8번 배치) payment-service SettlementBatch 가 채운다. 시드 단계에선 비워 둠(런타임 첫 배치가
-- 미정산 결제를 모아 처음 채운다).
CREATE TABLE IF NOT EXISTS settlement_summary (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    period_start   TIMESTAMP NOT NULL,
    period_end     TIMESTAMP NOT NULL,
    payment_count  INT NOT NULL,
    total_amount   DECIMAL(14,2) NOT NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- (8번 배치) restaurant-service PopularMenuBatch 가 매 주기 재계산해 채운다. 시드 단계에선 비워 둠.
CREATE TABLE IF NOT EXISTS menu_popularity_summary (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    menu_id        BIGINT NOT NULL,
    restaurant_id  BIGINT NOT NULL,
    menu_name      VARCHAR(128) NOT NULL,
    order_count    BIGINT NOT NULL,
    computed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- 기존 소량 데모 시드 (그대로 유지 — id 1~3 restaurants / 1~15 menus 는 하위호환 앵커)
-- ------------------------------------------------------------
INSERT IGNORE INTO menu_categories (id, name) VALUES
    (1, '분식'), (2, '중식'), (3, '치킨'), (4, '피자'),
    (5, '카페/디저트'), (6, '한식'), (7, '양식'), (8, '일식');

INSERT IGNORE INTO restaurants (id, name, region, status) VALUES
    (1, '강남 떡볶이 본점', 'GANGNAM', 'OPEN'),
    (2, '홍대 마라탕', 'HONGDAE', 'OPEN'),
    (3, '잠실 치킨하우스', 'JAMSIL', 'OPEN');

INSERT IGNORE INTO menus (id, restaurant_id, category_id, name, price, available) VALUES
    (1, 1, 1, '국물 떡볶이', 7000.00, TRUE),
    (2, 1, 1, '로제 떡볶이', 9000.00, TRUE),
    (3, 1, 1, '치즈 떡볶이', 8500.00, TRUE),
    (4, 1, 1, '순대 한 접시', 5000.00, TRUE),
    (5, 1, 1, '튀김 모듬', 6000.00, TRUE),
    (6, 2, 2, '기본 마라탕', 12000.00, TRUE),
    (7, 2, 2, '곱창 마라탕', 15000.00, TRUE),
    (8, 2, 2, '해물 마라탕', 14000.00, TRUE),
    (9, 2, 2, '마라샹궈', 18000.00, TRUE),
    (10, 2, 2, '꿔바로우', 13000.00, TRUE),
    (11, 3, 3, '후라이드 치킨', 18000.00, TRUE),
    (12, 3, 3, '양념 치킨', 19000.00, TRUE),
    (13, 3, 3, '간장 치킨', 19500.00, TRUE),
    (14, 3, 3, '치킨 무 (대)', 1500.00, TRUE),
    (15, 3, 3, '콜라 1.25L', 3000.00, TRUE);

-- ------------------------------------------------------------
-- 대량 시드 1: 식당 +20개(id 16~35, region/카테고리 다양화 — 읽기 API region/status 필터 재료)
-- ------------------------------------------------------------
INSERT INTO restaurants (id, name, region, status)
SELECT
    15 + n,
    CONCAT(ELT(1 + (n % 6), '분식집', '중국집', '치킨집', '피자가게', '카페', '고깃집'), ' ', n),
    ELT(1 + (n % 8), 'GANGNAM', 'HONGDAE', 'JAMSIL', 'YEOUIDO', 'MAPO', 'SONGPA', 'NOWON', 'GURO'),
    CASE WHEN RAND() < 0.1 THEN 'CLOSED' ELSE 'OPEN' END
FROM (
    WITH RECURSIVE seq AS (
        SELECT 1 AS n UNION ALL SELECT n + 1 FROM seq WHERE n < 20
    )
    SELECT n FROM seq
) t;

INSERT INTO menus (restaurant_id, category_id, name, price, available)
SELECT
    r.id,
    1 + (r.id % 8),
    CONCAT(ELT(1 + ((r.id + m.n) % 6), '시그니처 메뉴', '베스트 세트', '단품', '곱빼기', '사이드', '음료'), ' ', m.n),
    (5000 + FLOOR(RAND() * 20000)),
    RAND() >= 0.05
FROM restaurants r
JOIN (
    WITH RECURSIVE seq AS (
        SELECT 1 AS n UNION ALL SELECT n + 1 FROM seq WHERE n < 4
    )
    SELECT n FROM seq
) m ON TRUE
WHERE r.id > 15;

-- ------------------------------------------------------------
-- 대량 시드 2: 고객 2,000명(id 1~2000), 배달원 150명(id 1~150)
-- ------------------------------------------------------------
INSERT INTO customers (id, name, phone, email)
SELECT
    n,
    CONCAT(
        ELT(1 + (n % 10), '김', '이', '박', '최', '정', '강', '조', '윤', '장', '임'),
        ELT(1 + ((n DIV 10) % 10), '민준', '서연', '도윤', '지우', '하은', '시우', '유나', '건우', '서준', '지호')
    ),
    CONCAT('010-', LPAD((3000 + n) MOD 10000, 4, '0'), '-', LPAD(n MOD 10000, 4, '0')),
    CONCAT('customer', n, '@example.com')
FROM (
    WITH RECURSIVE seq AS (
        SELECT 1 AS n UNION ALL SELECT n + 1 FROM seq WHERE n < 2000
    )
    SELECT n FROM seq
) t;

INSERT INTO riders (id, name, phone, vehicle_type, status)
SELECT
    n,
    CONCAT('rider-', n),
    CONCAT('010-', LPAD((5000 + n) MOD 10000, 4, '0'), '-', LPAD(n MOD 10000, 4, '0')),
    ELT(1 + (n % 3), 'BIKE', 'SCOOTER', 'CAR'),
    CASE WHEN RAND() < 0.05 THEN 'INACTIVE' ELSE 'ACTIVE' END
FROM (
    WITH RECURSIVE seq AS (
        SELECT 1 AS n UNION ALL SELECT n + 1 FROM seq WHERE n < 150
    )
    SELECT n FROM seq
) t;

-- ------------------------------------------------------------
-- 대량 시드 3: 과거 주문 20,000건 (최근 90일, diurnal + 주말 가중 분포)
-- ------------------------------------------------------------
-- 시간 분포:
--  1) 요일: 50% 확률로 주말(금/토/일 균등), 50% 확률로 평일(월~목 균등) — 주말 하루당
--     밀도가 평일보다 약 1.3배 높아지는 가중.
--  2) 지난 13주(약 91일) 중 하나를 균등 선택해 실제 날짜를 정한다.
--  3) 시간대: 점심(11-13)·저녁(17-21) 피크에 가중치를 몰아준 6구간 순차 판정(각 WHEN의
--     RAND() 는 독립 호출 — 정확한 산술적 가중치가 아니라 "점심/저녁이 도드라지는" 현실적
--     모양이 목적이다. commerce의 width_bucket 누적가중치 방식과 달리 MySQL엔 width_bucket이
--     없어 순차 CASE로 대체).
DROP TEMPORARY TABLE IF EXISTS tmp_rolled_orders;
CREATE TEMPORARY TABLE tmp_rolled_orders AS
WITH RECURSIVE seq AS (
    SELECT 1 AS n UNION ALL SELECT n + 1 FROM seq WHERE n < 20000
)
SELECT
    n,
    1 + FLOOR(RAND() * 2000) AS customer_n,
    -- 식당 id 는 1~3(기존 데모) 과 16~35(대량 시드) 뿐이고 4~15 는 존재하지 않는다 — 두 대역을
    -- 개수 비례(3:20)로 가중해 섞는다.
    CASE WHEN RAND() < 3.0 / 23 THEN 1 + FLOOR(RAND() * 3) ELSE 16 + FLOOR(RAND() * 20) END AS restaurant_id,
    CASE WHEN RAND() < 0.05 THEN 'CANCELLED' ELSE 'DELIVERED' END AS status,
    CASE WHEN RAND() < 0.5
         THEN ELT(1 + FLOOR(RAND() * 3), 5, 6, 0)
         ELSE ELT(1 + FLOOR(RAND() * 4), 1, 2, 3, 4)
    END AS dow,
    FLOOR(RAND() * 13) AS week_offset,
    CASE
        WHEN RAND() < 0.35 THEN 17 + FLOOR(RAND() * 5)
        WHEN RAND() < 0.55 THEN 11 + FLOOR(RAND() * 3)
        WHEN RAND() < 0.70 THEN 6 + FLOOR(RAND() * 5)
        WHEN RAND() < 0.85 THEN 14 + FLOOR(RAND() * 3)
        WHEN RAND() < 0.95 THEN 22 + FLOOR(RAND() * 2)
        ELSE FLOOR(RAND() * 6)
    END AS hr,
    FLOOR(RAND() * 60) AS min_off,
    FLOOR(RAND() * 60) AS sec_off
FROM seq;

INSERT INTO orders (id, customer_id, restaurant_id, total_amount, status, created_at)
SELECT
    n + 1000,
    CONCAT('cust-', customer_n),
    restaurant_id,
    0,
    status,
    (CURDATE() - INTERVAL ((DAYOFWEEK(CURDATE()) - 1 - dow + 7) % 7) DAY)
        - INTERVAL (week_offset * 7) DAY
        + INTERVAL hr HOUR
        + INTERVAL min_off MINUTE
        + INTERVAL sec_off SECOND
FROM tmp_rolled_orders;

-- 주문당 1~3개 아이템, 해당 식당 소속 메뉴 중에서만 선택(식당별 메뉴 개수가 제각각이라
-- pick_seed(0~999999)를 식당별 0-based 순번으로 매핑하는 tmp_menu_rank 를 거친다).
DROP TABLE IF EXISTS tmp_order_items;
CREATE TABLE tmp_order_items AS
WITH RECURSIVE seq AS (
    SELECT 1 AS n UNION ALL SELECT n + 1 FROM seq WHERE n < 20000
)
SELECT
    n AS order_seq,
    1 + FLOOR(RAND() * 3) AS item_count,
    FLOOR(RAND() * 1000000) AS pick_seed_1,
    FLOOR(RAND() * 1000000) AS pick_seed_2,
    FLOOR(RAND() * 1000000) AS pick_seed_3,
    1 + FLOOR(RAND() * 3) AS qty_1,
    1 + FLOOR(RAND() * 3) AS qty_2,
    1 + FLOOR(RAND() * 3) AS qty_3
FROM seq;

DROP TABLE IF EXISTS tmp_menu_rank;
CREATE TABLE tmp_menu_rank AS
SELECT
    id AS menu_id,
    restaurant_id,
    price,
    ROW_NUMBER() OVER (PARTITION BY restaurant_id ORDER BY id) - 1 AS rn,
    COUNT(*) OVER (PARTITION BY restaurant_id) AS menu_count
FROM menus;

INSERT INTO order_items (order_id, menu_id, qty, unit_price)
SELECT o.id, mr.menu_id, i.qty_1, mr.price
FROM tmp_order_items i
JOIN orders o ON o.id = i.order_seq + 1000
JOIN tmp_menu_rank mr ON mr.restaurant_id = o.restaurant_id
    AND mr.rn = i.pick_seed_1 % mr.menu_count
UNION ALL
SELECT o.id, mr.menu_id, i.qty_2, mr.price
FROM tmp_order_items i
JOIN orders o ON o.id = i.order_seq + 1000
JOIN tmp_menu_rank mr ON mr.restaurant_id = o.restaurant_id
    AND mr.rn = i.pick_seed_2 % mr.menu_count
WHERE i.item_count >= 2
UNION ALL
SELECT o.id, mr.menu_id, i.qty_3, mr.price
FROM tmp_order_items i
JOIN orders o ON o.id = i.order_seq + 1000
JOIN tmp_menu_rank mr ON mr.restaurant_id = o.restaurant_id
    AND mr.rn = i.pick_seed_3 % mr.menu_count
WHERE i.item_count >= 3;

UPDATE orders o
JOIN (SELECT order_id, SUM(unit_price * qty) AS total FROM order_items GROUP BY order_id) sub
    ON o.id = sub.order_id
SET o.total_amount = sub.total;

-- 배차 히스토리: 과거 시드 데이터라 ETA 는 이미 지난 시각 — 배차 만료 배치가 다시 처리할
-- 필요가 없도록 DELIVERED 로 바로 시딩한다(운영 중 신규 주문만 ASSIGNED→배치 전이를 거친다).
INSERT INTO dispatches (order_id, courier_id, eta_minutes, status, assigned_at)
SELECT
    o.id,
    CONCAT('rider-', 1 + FLOOR(RAND() * 150)),
    15 + FLOOR(RAND() * 20),
    'DELIVERED',
    o.created_at
FROM orders o
WHERE o.id > 1000;

INSERT INTO dispatch_events (dispatch_id, status, occurred_at)
SELECT d.id, 'ASSIGNED', d.assigned_at FROM dispatches d WHERE d.order_id > 1000;

INSERT INTO dispatch_events (dispatch_id, status, occurred_at)
SELECT d.id, 'DELIVERED', d.assigned_at + INTERVAL d.eta_minutes MINUTE
FROM dispatches d WHERE d.order_id > 1000;

-- 결제 히스토리: 주문 상태를 그대로 반영(취소 주문 = 결제 실패 시나리오). 2시간 이내 건은
-- settled_at 을 비워 SettlementBatch 의 첫 실행 대상으로 남긴다.
INSERT INTO payments (order_id, pg_provider, amount, status, processed_at, settled_at, created_at)
SELECT
    o.id,
    ELT(1 + FLOOR(RAND() * 3), 'default', 'toss', 'kakaopay'),
    o.total_amount,
    CASE WHEN o.status = 'DELIVERED' THEN 'APPROVED' ELSE 'FAILED' END,
    o.created_at,
    CASE WHEN o.status = 'DELIVERED' AND o.created_at < NOW() - INTERVAL 2 HOUR
         THEN o.created_at + INTERVAL 1 HOUR ELSE NULL END,
    o.created_at
FROM orders o
WHERE o.id > 1000;

DROP TEMPORARY TABLE IF EXISTS tmp_rolled_orders;
DROP TABLE IF EXISTS tmp_order_items;
DROP TABLE IF EXISTS tmp_menu_rank;
