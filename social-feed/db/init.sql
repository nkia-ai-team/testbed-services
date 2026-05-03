-- social-feed schema (MySQL 8.0)
-- 4개 테이블: posts, feed_entries, comments, notifications

CREATE DATABASE IF NOT EXISTS socialfeed
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE socialfeed;

CREATE TABLE IF NOT EXISTS posts (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    author_id   BIGINT NOT NULL,
    content     TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_post_author (author_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS feed_entries (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    post_id     BIGINT NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_feed_user (user_id),
    INDEX idx_feed_post (post_id),
    CONSTRAINT fk_feed_post FOREIGN KEY (post_id) REFERENCES posts(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS comments (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    post_id     BIGINT NOT NULL,
    author_id   BIGINT NOT NULL,
    content     TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_comment_post (post_id),
    CONSTRAINT fk_comment_post FOREIGN KEY (post_id) REFERENCES posts(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS notifications (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    type        VARCHAR(32),
    ref_id      BIGINT,
    read_flag   TINYINT DEFAULT 0,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_notif_user (user_id)
) ENGINE=InnoDB;

-- ============================================================
-- 시드 데이터: 사용자 1~10, 게시물 20, fan-out 피드, 댓글, 알림
-- ============================================================

INSERT INTO posts (id, author_id, content) VALUES
(1, 1, '오늘 출근길 단풍이 너무 예뻤다'),
(2, 1, '점심 김치찌개 맛집 추천 받습니다'),
(3, 2, '주말 등산 다녀왔어요. 북한산 코스 추천!'),
(4, 2, '카페에서 작업 중. 노트북 거치대 좋네'),
(5, 3, '운동 100일째. 변화가 보인다'),
(6, 3, '아이폰 17 프로 사야하나 고민중...'),
(7, 4, '집들이 메뉴 추천해주세요'),
(8, 4, '신혼집 인테리어 진행 중'),
(9, 5, '서울 출장 마무리. 내일은 부산'),
(10, 5, '맛있는 평양냉면집 추천 받습니다'),
(11, 6, '강아지 산책 사진'),
(12, 6, '비 오는 날 카페 분위기 최고'),
(13, 7, '독서 모임 후기'),
(14, 7, '책 추천: 사피엔스'),
(15, 8, '제주도 여행 후기'),
(16, 8, '비행기 티켓 예매 꿀팁'),
(17, 9, '재택근무 책상 셋업'),
(18, 9, '키보드 타건감 비교'),
(19, 10, '주말 캠핑 다녀왔습니다'),
(20, 10, 'viral! 이거 보세요');

-- fan-out: 각 게시물이 여러 사용자 피드에 들어감
INSERT INTO feed_entries (user_id, post_id) VALUES
(1, 1), (2, 1), (3, 1), (4, 1), (5, 1),
(1, 2), (2, 2), (3, 2),
(2, 3), (3, 3), (4, 3), (5, 3),
(2, 4), (3, 4),
(3, 5), (4, 5), (5, 5),
(3, 6), (4, 6),
(4, 7), (5, 7), (6, 7),
(4, 8), (5, 8),
(5, 9), (6, 9), (7, 9),
(5, 10), (6, 10),
(6, 11), (7, 11), (8, 11),
(6, 12), (7, 12),
(7, 13), (8, 13), (9, 13),
(7, 14), (8, 14),
(8, 15), (9, 15), (10, 15),
(8, 16), (9, 16),
(9, 17), (10, 17), (1, 17),
(9, 18), (10, 18),
(10, 19), (1, 19), (2, 19),
(10, 20), (1, 20), (2, 20), (3, 20), (4, 20), (5, 20), (6, 20), (7, 20), (8, 20), (9, 20);

INSERT INTO comments (post_id, author_id, content) VALUES
(1, 2, '저도 봤어요'),
(1, 3, '단풍 시즌 끝났네요'),
(2, 4, '광화문 ㅇㅇ식당 추천'),
(3, 5, '부럽네요'),
(5, 1, '대단하시네요'),
(7, 6, '잡채 추천'),
(15, 9, '제주도 좋죠'),
(20, 1, '와 대박'),
(20, 2, '믿기지 않네요'),
(20, 3, 'viral 인정');

INSERT INTO notifications (user_id, type, ref_id, read_flag) VALUES
(1, 'COMMENT', 1, 0),
(1, 'LIKE', 2, 1),
(2, 'COMMENT', 3, 0),
(2, 'FOLLOW', 5, 0),
(3, 'COMMENT', 5, 1),
(4, 'LIKE', 7, 0),
(5, 'FOLLOW', 1, 1),
(6, 'COMMENT', 11, 0),
(7, 'LIKE', 13, 0),
(8, 'COMMENT', 15, 0);
