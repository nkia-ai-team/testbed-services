-- ============================================================
-- 통합 시딩 스크립트 (docker-entrypoint-initdb.d / K8s postgres-init-scripts 공용)
-- ============================================================
-- §6(스키마 확장) 대량 시드: 유저 ~3,000 / 상품 ~2,000(+variants) / 과거 주문 ~50,000
-- (order_items·payments·shipments 정합) — 챗봇 추세·집계 질의, 이상감지 계절성 학습 재료.
--
-- 재현성: setseed()로 RNG를 고정한다(단, 병렬 쿼리 워커가 붙으면 random() 호출 순서가
-- 흔들려 완전한 바이트 단위 재현은 보장되지 않는다 — 분포 특성 재현이 목적이지 초정밀
-- 재현은 아니다).
--
-- ID 대역 분리(기존 소량 시드와 충돌 방지):
--   products/users: 1~20(또는 1~16)은 기존 데모 시드, 1001~ 부터 대량 생성분.
--   orders/order_items/payments/shipments/shipment_events/product_variants/inventory_movements/
--   cart/cart_items: 이 테이블들은 대량 시드 이전엔 비어 있었으므로 별도 대역 분리 불필요.
-- 대량 생성 후에는 products/users/orders 시퀀스를 MAX(id)로 setval해, 이후 애플리케이션이
-- 만드는 새 행이 시드 ID와 충돌하지 않게 한다.

SELECT setseed(0.42);

-- ------------------------------------------------------------
-- 스키마 생성 (8개 서비스)
-- ------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS order_schema;
CREATE SCHEMA IF NOT EXISTS product_schema;
CREATE SCHEMA IF NOT EXISTS inventory_schema;
CREATE SCHEMA IF NOT EXISTS payment_schema;
CREATE SCHEMA IF NOT EXISTS user_schema;
CREATE SCHEMA IF NOT EXISTS cart_schema;
CREATE SCHEMA IF NOT EXISTS pricing_schema;
CREATE SCHEMA IF NOT EXISTS shipping_schema;

-- ------------------------------------------------------------
-- 기존 소량 데모 시드 (그대로 유지 — flagship 16개 상품이 주문/견적/재고 흐름의 앵커)
-- ------------------------------------------------------------
INSERT INTO product_schema.categories (id, name, description) VALUES
(1, '기기', '전자담배 본체, 팟 디바이스, 모드 기기'),
(2, '액상', '니코틴 액상, 무니코틴 액상, 프리미엄 리퀴드'),
(3, '코일/팟', '교체용 코일, 팟 카트리지, 메쉬 코일'),
(4, '악세서리', '배터리, 충전기, 드립팁, 케이스')
ON CONFLICT (id) DO NOTHING;

INSERT INTO product_schema.products (id, category_id, name, description, price) VALUES
(1, 1, 'PlopVape Air Pro', '초경량 팟 디바이스, 850mAh 배터리, Type-C 충전', 45000),
(2, 1, 'PlopVape Box Mod 200W', '듀얼 배터리 모드, 온도 제어, OLED 디스플레이', 89000),
(3, 1, 'PlopVape Stick V2', '올인원 스틱형, 초보자용, 1100mAh', 32000),
(4, 1, 'PlopVape Pod Mini', '미니 팟 디바이스, 자동 흡입 감지, 휴대용', 28000),
(5, 2, '클래식 버지니아 30ml', '진한 버지니아 블렌드, 니코틴 9mg', 15000),
(6, 2, '아이스 멘솔 50ml', '쿨링 멘솔, 니코틴 6mg, 대용량', 22000),
(7, 2, '망고 패션프루트 30ml', '열대과일 블렌드, 니코틴 3mg', 18000),
(8, 2, '카페라떼 30ml', '커피+크림 디저트 라인, 무니코틴', 16000),
(9, 3, 'Air Pro 교체 팟 (4입)', 'Air Pro 전용, 1.0ohm 메쉬 코일 내장', 12000),
(10, 3, 'Box Mod 메쉬 코일 (5입)', '0.15ohm 메쉬 코일, 고출력용', 18000),
(11, 3, 'Stick V2 교체 코일 (3입)', '0.8ohm MTL 코일, 입호흡용', 9000),
(12, 3, 'Pod Mini 카트리지 (3입)', '1.2ohm 세라믹 코일, 액상 누수 방지', 10000),
(13, 4, '18650 배터리 2개입', '3000mAh 고방전 배터리, Box Mod 호환', 16000),
(14, 4, 'USB-C 고속 충전 케이블', '1.5m 길이, 전 기기 호환', 8000),
(15, 4, '가죽 파우치 케이스', '기기+액상 수납, PlopVape 로고 각인', 25000),
(16, 4, '레진 드립팁 세트 (3종)', '510 규격, 핸드메이드 레진', 15000)
ON CONFLICT (id) DO NOTHING;

INSERT INTO inventory_schema.inventory (id, product_id, stock, reserved) VALUES
(1, 1, 50, 0), (2, 2, 30, 0), (3, 3, 80, 0), (4, 4, 100, 0),
(5, 5, 200, 0), (6, 6, 150, 0), (7, 7, 180, 0), (8, 8, 120, 0),
(9, 9, 300, 0), (10, 10, 250, 0), (11, 11, 400, 0), (12, 12, 350, 0),
(13, 13, 100, 0), (14, 14, 500, 0), (15, 15, 60, 0), (16, 16, 200, 0)
ON CONFLICT (id) DO NOTHING;

INSERT INTO pricing_schema.prices (id, product_id, base_price) VALUES
(1, 1, 45000), (2, 2, 89000), (3, 3, 32000), (4, 4, 28000),
(5, 5, 15000), (6, 6, 22000), (7, 7, 18000), (8, 8, 16000),
(9, 9, 12000), (10, 10, 18000), (11, 11, 9000), (12, 12, 10000),
(13, 13, 16000), (14, 14, 8000), (15, 15, 25000), (16, 16, 15000)
ON CONFLICT (id) DO NOTHING;

INSERT INTO pricing_schema.promotions (id, name, description, discount_percent, starts_at, ends_at, active) VALUES
(1, '봄맞이 세일', '전 품목 10% 할인', 10.00, '2026-01-01 00:00:00', '2026-12-31 23:59:59', true),
(2, '신규가입 이벤트', '가입 첫 구매 5% 추가 할인', 5.00, '2026-01-01 00:00:00', '2026-12-31 23:59:59', true),
(3, '겨울 시즌 종료 세일', '재고 정리 15% 할인', 15.00, '2025-01-01 00:00:00', '2025-02-28 23:59:59', false)
ON CONFLICT (id) DO NOTHING;

INSERT INTO pricing_schema.coupons (id, code, discount_amount, discount_percent, active) VALUES
(1, 'WELCOME5000', 5000, 0, true),
(2, 'SAVE10', 0, 10.00, true),
(3, 'VIP20', 0, 20.00, true),
(4, 'EXPIRED1000', 1000, 0, false)
ON CONFLICT (id) DO NOTHING;

INSERT INTO user_schema.users (id, email, password_hash, name, phone) VALUES
(1, 'user01@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '김민준', '010-1001-0001'),
(2, 'user02@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '이서연', '010-1001-0002'),
(3, 'user03@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '박도윤', '010-1001-0003'),
(4, 'user04@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '최지우', '010-1001-0004'),
(5, 'user05@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '정하은', '010-1001-0005'),
(6, 'user06@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '강시우', '010-1001-0006'),
(7, 'user07@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '조유나', '010-1001-0007'),
(8, 'user08@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '윤건우', '010-1001-0008'),
(9, 'user09@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '장서준', '010-1001-0009'),
(10, 'user10@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '임지호', '010-1001-0010'),
(11, 'user11@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '한소율', '010-1001-0011'),
(12, 'user12@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '오예준', '010-1001-0012'),
(13, 'user13@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '서지안', '010-1001-0013'),
(14, 'user14@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '신하율', '010-1001-0014'),
(15, 'user15@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '권도현', '010-1001-0015'),
(16, 'user16@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '황서윤', '010-1001-0016'),
(17, 'user17@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '안민서', '010-1001-0017'),
(18, 'user18@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '송준우', '010-1001-0018'),
(19, 'user19@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '유아린', '010-1001-0019'),
(20, 'user20@example.com', '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c', '전시현', '010-1001-0020')
ON CONFLICT (id) DO NOTHING;

INSERT INTO user_schema.addresses (id, user_id, recipient_name, phone, zipcode, address1, address2, is_default) VALUES
(1, 1, '김민준', '010-1001-0001', '06236', '서울특별시 강남구 테헤란로 123', '101동 1001호', true),
(2, 2, '이서연', '010-1001-0002', '04524', '서울특별시 중구 세종대로 110', '', true),
(3, 3, '박도윤', '010-1001-0003', '48058', '부산광역시 해운대구 centum중앙로 55', '3층', true),
(4, 4, '최지우', '010-1001-0004', '35240', '대전광역시 서구 둔산로 100', '', true),
(5, 5, '정하은', '010-1001-0005', '61472', '광주광역시 서구 상무중앙로 76', '', true)
ON CONFLICT (id) DO NOTHING;

-- ------------------------------------------------------------
-- 대량 시드 1: 상품 ~2,000개 (id 1001~3000) + variants 2~4개씩
-- ------------------------------------------------------------
INSERT INTO product_schema.products (id, category_id, name, description, price)
SELECT
    1000 + n AS id,
    1 + (n % 4) AS category_id,
    (ARRAY['PlopVape', 'VaporX', 'CloudMax', 'MistPro'])[1 + (n % 4)] || ' ' ||
    (ARRAY['Air', 'Nova', 'Edge', 'Slim', 'Turbo', 'Zen'])[1 + (n % 6)] || ' ' || (n % 100) AS name,
    '자동 생성 시드 상품 #' || n AS description,
    (5000 + floor(random() * 95000))::numeric AS price
FROM generate_series(1, 2000) AS n
ON CONFLICT (id) DO NOTHING;

SELECT setval(pg_get_serial_sequence('product_schema.products', 'id'), (SELECT MAX(id) FROM product_schema.products));

INSERT INTO product_schema.product_variants (product_id, sku, variant_name, price_delta)
SELECT
    p.id,
    'SKU-' || p.id || '-' || v.variant_no,
    (ARRAY['블랙', '화이트', '블루', '레드'])[1 + ((p.id + v.variant_no) % 4)] || ' / ' ||
    (ARRAY['소형', '대형'])[1 + (v.variant_no % 2)],
    CASE WHEN v.variant_no = 1 THEN 0 ELSE v.variant_no * 1000 END
FROM product_schema.products p
CROSS JOIN LATERAL generate_series(1, (2 + (p.id % 3))::int) AS v(variant_no)
ON CONFLICT (sku) DO NOTHING;

-- ------------------------------------------------------------
-- 대량 시드 2: 유저 ~3,000명 (id 1001~4000), 비밀번호는 기존 시드와 동일하게 'Passw0rd!'
-- ------------------------------------------------------------
INSERT INTO user_schema.users (id, email, password_hash, name, phone)
SELECT
    1000 + n AS id,
    'bulkuser' || n || '@example.com' AS email,
    '4e1b0d41f7c7252ebcbc29e72615a46c971c86be018780d7f88d68264b22024c' AS password_hash,
    (ARRAY['김', '이', '박', '최', '정', '강', '조', '윤', '장', '임'])[1 + (n % 10)] ||
    (ARRAY['민준', '서연', '도윤', '지우', '하은', '시우', '유나', '건우', '서준', '지호'])[1 + ((n / 10) % 10)] AS name,
    '010-' || lpad((2000 + n)::text, 4, '0') || '-' || lpad((n % 10000)::text, 4, '0') AS phone
FROM generate_series(1, 3000) AS n
ON CONFLICT (id) DO NOTHING;

SELECT setval(pg_get_serial_sequence('user_schema.users', 'id'), (SELECT MAX(id) FROM user_schema.users));

-- 대량 유저 중 10%(약 300명)에게 장바구니를 부여 — cart-service 시드가 대량 유저와 정합되게.
INSERT INTO cart_schema.carts (user_id)
SELECT 1000 + n FROM generate_series(1, 3000, 10) AS n
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO cart_schema.cart_items (cart_id, product_id, quantity)
SELECT c.id, pick.product_id, pick.qty
FROM cart_schema.carts c
CROSS JOIN LATERAL (SELECT c.id AS _corr, (1 + floor(random() * 3))::int AS item_count) ic
CROSS JOIN LATERAL generate_series(1, ic.item_count) AS item_no
CROSS JOIN LATERAL (
    -- pick을 item_no(가장 안쪽 루프)에 상관시켜야 같은 cart의 아이템끼리 상품이 겹치지 않는다.
    SELECT item_no AS _corr, (1 + floor(random() * 16))::bigint AS product_id, (1 + floor(random() * 3))::int AS qty
) pick
WHERE c.user_id > 1000
ON CONFLICT (cart_id, product_id) DO NOTHING;

-- ------------------------------------------------------------
-- 대량 시드 3: 과거 주문 ~50,000건 (최근 90일, diurnal + 주말 가중 분포)
-- ------------------------------------------------------------
-- 시간 분포는 두 단계로 뽑는다:
--  1) 요일(dow, 0=일~6=토)을 가중 선택 — 주말(금/토/일)이 평일보다 높게.
--     가중치 [일10, 월7, 화7, 수7, 목7, 금9, 토12] 합계59.
--  2) 그 요일에 해당하는 지난 13주(약 91일) 중 한 주를 균등 선택해 실제 날짜를 정한다.
--  3) 시(hour)는 낮/저녁이 높고 새벽이 낮은 24버킷 가중치로 선택.
--     가중치 [1,1,1,1,1,2,3,5,6,7,8,8,7,7,7,8,8,9,8,7,6,4,3,2] 합계120.
-- width_bucket(random()*total, cumulative_weights)로 가중 샘플링(조인 없이 벡터 연산이라 5만행도 빠르다).
CREATE TEMP TABLE tmp_orders AS
SELECT
    gs.n AS id,
    pick.user_id,
    u.name AS customer_name,
    u.email AS customer_email,
    ts.order_ts AS created_at,
    CASE WHEN random() < 0.05 THEN 'PAYMENT_FAILED' ELSE 'PAID' END AS status
FROM generate_series(1, 50000) AS gs(n)
-- 주의: LATERAL 서브쿼리가 바깥 행(gs.n)을 전혀 참조하지 않으면 PostgreSQL 플래너가
-- "상관관계 없음"으로 판단해 한 번만 평가한 뒤 모든 바깥 행에 그 결과를 재사용해버린다
-- (random()이 VOLATILE이어도 예외 없이 발생 — 실사용 중 실제로 5만 행이 전부 동일한 값으로
-- 무너지는 걸 확인했다). 그래서 각 LATERAL의 SELECT 목록에 gs.n을 더미로 끼워 넣어
-- "진짜 상관 서브쿼리"로 만들어 행마다 재평가되게 강제한다.
CROSS JOIN LATERAL (
    -- 기존 데모 유저 20명과 대량 유저 3000명을 하나의 풀(3020명)로 취급해 균등 추출.
    SELECT gs.n AS _corr, CASE WHEN random() < (20.0 / 3020)
                THEN (1 + floor(random() * 20))::bigint
                ELSE (1001 + floor(random() * 3000))::bigint
           END AS user_id
) pick
JOIN user_schema.users u ON u.id = pick.user_id
CROSS JOIN LATERAL (
    SELECT
        gs.n AS _corr,
        width_bucket(random() * 59, ARRAY[10, 17, 24, 31, 38, 47, 59]::numeric[]) AS dow,
        floor(random() * 13)::int AS week_offset,
        width_bucket(random() * 120,
            ARRAY[1, 2, 3, 4, 5, 7, 10, 15, 21, 28, 36, 44, 51, 58, 65, 73, 81, 90, 98, 105, 111, 115, 118, 120]::numeric[]
        ) AS hr
) w
CROSS JOIN LATERAL (
    SELECT
        ((CURRENT_DATE - ((EXTRACT(DOW FROM CURRENT_DATE)::int - w.dow + 7) % 7)) - (w.week_offset * 7))
            + (LEAST(w.hr, 23) || ' hours')::interval
            + (floor(random() * 60) || ' minutes')::interval
            + (floor(random() * 60) || ' seconds')::interval AS order_ts
) ts;

INSERT INTO order_schema.orders (id, user_id, customer_name, customer_email, total_amount, status, created_at, updated_at)
SELECT id, user_id, customer_name, customer_email, 0, status, created_at, created_at
FROM tmp_orders;

SELECT setval(pg_get_serial_sequence('order_schema.orders', 'id'), (SELECT MAX(id) FROM order_schema.orders));

-- 주문당 1~3개 아이템, flagship 16개 상품(id 1~16)만 사용 — 이 상품들만 product/pricing/inventory
-- 3개 서비스에 걸쳐 완전히 정합된 데이터를 갖고 있어 히스토리 시뮬레이션 대상으로 삼았다.
INSERT INTO order_schema.order_items (order_id, product_id, product_name, quantity, unit_price, subtotal)
SELECT o.id, p.id, p.name, pick.qty, p.price, p.price * pick.qty
FROM tmp_orders o
-- item_count는 o.id에, pick은 item_no(가장 안쪽 루프 변수)에 상관시킨다 — pick이 o.id에만
-- 상관되면 같은 주문 안의 item_no 1/2/3 사이에서 재사용(캐시)되어 매 아이템이 같은 상품으로
-- 무너지는 걸 실측으로 확인했다(위 tmp_orders 캐싱 버그의 한 단계 더 깊은 변종).
CROSS JOIN LATERAL (SELECT o.id AS _corr, (1 + floor(random() * 3))::int AS item_count) ic
CROSS JOIN LATERAL generate_series(1, ic.item_count) AS item_no
CROSS JOIN LATERAL (
    SELECT item_no AS _corr, (1 + floor(random() * 16))::bigint AS product_id, (1 + floor(random() * 3))::int AS qty
) pick
JOIN product_schema.products p ON p.id = pick.product_id;

UPDATE order_schema.orders o
SET total_amount = sub.total
FROM (SELECT order_id, SUM(subtotal) AS total FROM order_schema.order_items GROUP BY order_id) sub
WHERE o.id = sub.order_id;

-- 결제 히스토리: 주문 상태를 그대로 반영. 2시간 이내 건은 settled_at을 비워 settlement 배치의
-- 첫 실행 대상으로 남긴다(그 이전 건은 이미 지난 배치 주기에 정산된 것으로 간주).
INSERT INTO payment_schema.payments (order_id, amount, method, status, pg_transaction_id, settled_at, created_at)
SELECT
    o.id,
    o.total_amount,
    (ARRAY['CARD', 'CARD', 'CARD', 'BANK_TRANSFER'])[1 + floor(random() * 4)::int],
    CASE WHEN o.status = 'PAID' THEN 'SUCCESS' ELSE 'FAILED' END,
    'seed-tx-' || o.id,
    CASE WHEN o.status = 'PAID' AND o.created_at < NOW() - INTERVAL '2 hours'
         THEN o.created_at + INTERVAL '1 hour' ELSE NULL END,
    o.created_at
FROM order_schema.orders o;

-- 배송 히스토리: PAID 주문만. 주문 경과일에 따라 CREATED→SHIPPED→DELIVERED 중 하나로 정착.
INSERT INTO shipping_schema.shipments (order_id, status, tracking_number, carrier, created_at, updated_at)
SELECT
    o.id,
    CASE
        WHEN o.created_at < NOW() - INTERVAL '7 days' THEN 'DELIVERED'
        WHEN o.created_at < NOW() - INTERVAL '2 days' THEN 'SHIPPED'
        ELSE 'CREATED'
    END,
    'SEED' || lpad(o.id::text, 8, '0'),
    (ARRAY['CJ대한통운', '한진택배', '롯데택배', '우체국택배'])[1 + floor(random() * 4)::int],
    o.created_at,
    o.created_at
FROM order_schema.orders o
WHERE o.status = 'PAID';

INSERT INTO shipping_schema.shipment_events (shipment_id, status, occurred_at)
SELECT s.id, 'CREATED', s.created_at
FROM shipping_schema.shipments s;

INSERT INTO shipping_schema.shipment_events (shipment_id, status, occurred_at)
SELECT s.id, 'SHIPPED', s.created_at + INTERVAL '1 day'
FROM shipping_schema.shipments s
WHERE s.status IN ('SHIPPED', 'DELIVERED');

INSERT INTO shipping_schema.shipment_events (shipment_id, status, occurred_at)
SELECT s.id, 'DELIVERED', s.created_at + INTERVAL '4 days'
FROM shipping_schema.shipments s
WHERE s.status = 'DELIVERED';

DROP TABLE IF EXISTS tmp_orders;
