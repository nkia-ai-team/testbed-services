package com.corebanking.common.outbox;

import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.LocalDateTime;
import java.util.List;

public interface OutboxEventRepository extends JpaRepository<OutboxEvent, Long> {
    List<OutboxEvent> findTop100ByPublishedAtIsNullOrderByCreatedAtAsc();

    @Modifying
    @Query("update OutboxEvent e set e.publishedAt = :publishedAt where e.id in :ids")
    int markPublished(@Param("ids") List<Long> ids, @Param("publishedAt") LocalDateTime publishedAt);

    /** 정리 대상 판정에 필요한 두 컬럼만 읽는다 — 엔티티로 읽으면 payload(CLOB)까지 끌려온다. */
    interface Head {
        Long getId();

        LocalDateTime getPublishedAt();
    }

    @Query("select e.id as id, e.publishedAt as publishedAt from OutboxEvent e order by e.id asc")
    List<Head> findOldest(Pageable pageable);
}
