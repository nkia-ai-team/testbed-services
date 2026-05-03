package com.nkia.socialfeed.feed.repository;

import com.nkia.socialfeed.feed.entity.FeedEntry;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

import java.util.List;

public interface FeedEntryRepository extends JpaRepository<FeedEntry, Long> {

    /**
     * 사용자 피드 조회.
     *
     * failure_surface: db-cpu-throttle.
     * 의도적으로 인덱스 회피 — full table scan 유도. content 컬럼은 posts 테이블에 있어
     * native query 로 cross-table join 하면서 LIKE 절로 풀스캔 트리거 가능한 구조.
     * 일상 부하: idx_feed_user 가 있어 빠르게 동작. 시나리오 시: posts.content LIKE '%...%'
     * 추가하면 풀스캔 + CPU 스파이크.
     */
    @Query(value = """
            SELECT f.id AS id,
                   f.user_id AS user_id,
                   f.post_id AS post_id,
                   p.content AS content,
                   p.author_id AS author_id,
                   f.created_at AS created_at
            FROM feed_entries f
            JOIN posts p ON p.id = f.post_id
            WHERE f.user_id = :userId
            ORDER BY f.id DESC
            LIMIT 50
            """, nativeQuery = true)
    List<Object[]> findFeedRowsByUserId(Long userId);
}
