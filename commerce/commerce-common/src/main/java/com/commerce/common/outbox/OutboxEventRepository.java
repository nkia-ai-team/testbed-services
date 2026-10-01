package com.commerce.common.outbox;

import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

import java.time.LocalDateTime;
import java.util.List;

public interface OutboxEventRepository extends JpaRepository<OutboxEvent, Long> {

    List<OutboxEvent> findTop100ByPublishedAtIsNullOrderByCreatedAtAsc();

    /** 정리 대상 판정에 필요한 두 컬럼만 읽는다 — 엔티티로 읽으면 payload(TEXT)까지 끌려온다. */
    interface Head {
        Long getId();

        LocalDateTime getPublishedAt();
    }

    @Query("select e.id as id, e.publishedAt as publishedAt from OutboxEvent e order by e.id asc")
    List<Head> findOldest(Pageable pageable);
}
