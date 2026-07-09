-- core-banking schema (MariaDB)
-- 3개 테이블: accounts, transfers, ledger_entries
-- 이체(transfer)는 accounts 를 FOR UPDATE 로 lock → row-lock 경합 표면.
-- ledger_entries 는 이체 이벤트를 복식부기로 반영(worker).

CREATE DATABASE IF NOT EXISTS banking
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE banking;

CREATE TABLE IF NOT EXISTS accounts (
    id          VARCHAR(64) PRIMARY KEY,
    holder      VARCHAR(128) NOT NULL,
    balance     DECIMAL(18,2) NOT NULL DEFAULT 0,
    status      VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS transfers (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    transfer_ref  VARCHAR(64) NOT NULL UNIQUE,
    from_account  VARCHAR(64) NOT NULL,
    to_account    VARCHAR(64) NOT NULL,
    amount        DECIMAL(18,2) NOT NULL,
    status        VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    order_id      VARCHAR(64),
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_transfers_from (from_account),
    INDEX idx_transfers_to (to_account),
    INDEX idx_transfers_order (order_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ledger_entries (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    transfer_ref  VARCHAR(64) NOT NULL,
    account_id    VARCHAR(64) NOT NULL,
    direction     VARCHAR(8) NOT NULL,
    amount        DECIMAL(18,2) NOT NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_ledger_ref (transfer_ref),
    INDEX idx_ledger_account (account_id)
) ENGINE=InnoDB;

-- ============================================================
-- 시드 데이터: 계좌 12개(개인/법인 혼합, 잔액 보유)
-- ============================================================
INSERT INTO accounts (id, holder, balance, status) VALUES
    ('ACC-1001', '김민수',        5000000.00, 'ACTIVE'),
    ('ACC-1002', '이서연',        3200000.00, 'ACTIVE'),
    ('ACC-1003', '박지훈',        1500000.00, 'ACTIVE'),
    ('ACC-1004', '최유진',         800000.00, 'ACTIVE'),
    ('ACC-1005', '정우성',       12000000.00, 'ACTIVE'),
    ('ACC-1006', '한소희',         450000.00, 'ACTIVE'),
    ('ACC-1007', '오세훈',        2750000.00, 'ACTIVE'),
    ('ACC-2001', '플롭베이프(주)', 88000000.00, 'ACTIVE'),
    ('ACC-2002', '누리 커머스',   45000000.00, 'ACTIVE'),
    ('ACC-2003', '배달의고수',    23000000.00, 'ACTIVE'),
    ('ACC-9001', '동결계좌 A',      100000.00, 'FROZEN'),
    ('ACC-9002', '해지계좌 B',           0.00, 'CLOSED')
ON DUPLICATE KEY UPDATE holder = VALUES(holder);
