package com.corebanking.common.outbox;

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
}
