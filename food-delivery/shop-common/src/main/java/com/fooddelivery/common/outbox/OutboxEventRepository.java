package com.fooddelivery.common.outbox;

import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.NoRepositoryBean;

import java.time.LocalDateTime;
import java.util.List;

/**
 * MySQL 단일 DB(스키마 분리 없음)라 서비스마다 별도 테이블(order_outbox_events 등)을 쓴다 —
 * commerce처럼 "같은 테이블명 + 다른 schema" 트릭을 쓸 수 없어 제네릭으로 구현하고,
 * 각 서비스가 구체 엔티티(T)에 대한 구체 리포지토리 인터페이스를 선언한다.
 */
@NoRepositoryBean
public interface OutboxEventRepository<T extends OutboxEvent> extends JpaRepository<T, Long> {

    List<T> findTop100ByPublishedAtIsNullOrderByCreatedAtAsc();

    /** 정리 대상 판정에 필요한 두 컬럼만 읽는다 — 엔티티로 읽으면 payload(TEXT)까지 끌려온다. */
    interface Head {
        Long getId();

        LocalDateTime getPublishedAt();
    }

    @Query("select e.id as id, e.publishedAt as publishedAt from #{#entityName} e order by e.id asc")
    List<Head> findOldest(Pageable pageable);
}
