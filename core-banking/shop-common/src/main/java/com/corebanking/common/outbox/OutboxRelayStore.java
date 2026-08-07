package com.corebanking.common.outbox;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

/**
 * OutboxRelay 의 DB 접근만 모은 별도 빈. 별도 빈인 것이 핵심이다 — 같은 빈 안의
 * @Transactional 메서드는 self-invocation 이라 프록시를 타지 않아 트랜잭션 경계가
 * 생기지 않는다. 여기의 각 메서드가 곧 릴레이가 커넥션을 쥐는 구간이고, 그 사이의
 * Kafka 발행은 어느 트랜잭션에도 속하지 않는다.
 */
@Component
@ConditionalOnProperty(prefix = "outbox.relay", name = "enabled", havingValue = "true")
class OutboxRelayStore {

    private final OutboxEventRepository outboxEventRepository;
    private final OutboxRelayControlRepository controlRepository;

    OutboxRelayStore(OutboxEventRepository outboxEventRepository, OutboxRelayControlRepository controlRepository) {
        this.outboxEventRepository = outboxEventRepository;
        this.controlRepository = controlRepository;
    }

    @Transactional(readOnly = true)
    Optional<OutboxRelayControl> findControl(String controlId) {
        return controlRepository.findById(controlId);
    }

    @Transactional(readOnly = true)
    List<OutboxEvent> findPending() {
        return outboxEventRepository.findTop100ByPublishedAtIsNullOrderByCreatedAtAsc();
    }

    /** 성공분만 한 번의 UPDATE 로 마킹한다(건당 save 는 커넥션 점유 시간을 배치 크기만큼 늘린다). */
    @Transactional
    void markPublished(List<Long> ids, LocalDateTime publishedAt) {
        outboxEventRepository.markPublished(ids, publishedAt);
    }
}
