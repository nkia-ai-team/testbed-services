package com.fooddelivery.common.outbox;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.repository.NoRepositoryBean;

import java.util.List;

/**
 * MySQL 단일 DB(스키마 분리 없음)라 서비스마다 별도 테이블(order_outbox_events 등)을 쓴다 —
 * commerce처럼 "같은 테이블명 + 다른 schema" 트릭을 쓸 수 없어 제네릭으로 구현하고,
 * 각 서비스가 구체 엔티티(T)에 대한 구체 리포지토리 인터페이스를 선언한다.
 */
@NoRepositoryBean
public interface OutboxEventRepository<T extends OutboxEvent> extends JpaRepository<T, Long> {

    List<T> findTop100ByPublishedAtIsNullOrderByCreatedAtAsc();
}
