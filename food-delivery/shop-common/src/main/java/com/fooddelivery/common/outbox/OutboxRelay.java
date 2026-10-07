package com.fooddelivery.common.outbox;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 미발행 outbox 행을 주기적으로 폴링해 Kafka로 발행하는 릴레이 베이스. 서비스별 구체
 * 서브클래스가 @Component + @ConditionalOnProperty(outbox.relay.enabled=true) + @Scheduled로
 * 감싸서 등록한다(shop-common은 컴포넌트 스캔 대상 밖이라 직접 @Component를 못 둔다 —
 * 각 서비스가 명시적으로 스캔 범위에 포함시키거나 서브클래스를 자기 패키지에 둔다).
 * 발행 실패 시 published_at을 마킹하지 않으므로 다음 폴링 주기에 재시도된다 — at-least-once.
 *
 * <p>발행이 끝난 행은 purgePublished() 가 보존 기간 뒤에 지운다. 발행된 행을 다시 읽는
 * 곳은 없는데 지우지 않으면 테이블이 유입량만큼 끝없이 자란다 — 2026-10-01 실측으로
 * outbox 3개 테이블이 합쳐 2.7GB(전체의 절반)였고, 같은 결함으로 core-banking 은
 * Oracle Free 의 12GB 상한에 닿아 이체 기록이 37시간 멎었다.
 */
public abstract class OutboxRelay<T extends OutboxEvent> {

    private static final Logger log = LoggerFactory.getLogger(OutboxRelay.class);

    private final OutboxEventRepository<T> outboxEventRepository;
    private final KafkaTemplate<String, String> kafkaTemplate;

    // 서브클래스 3개의 생성자를 건드리지 않으려고 필드 주입으로 받는다.
    @Value("${outbox.purge.enabled:true}")
    private boolean purgeEnabled;
    @Value("${outbox.purge.retention-hours:24}")
    private long purgeRetentionHours;
    @Value("${outbox.purge.batch-size:500}")
    private int purgeBatchSize;

    protected OutboxRelay(OutboxEventRepository<T> outboxEventRepository, KafkaTemplate<String, String> kafkaTemplate) {
        this.outboxEventRepository = outboxEventRepository;
        this.kafkaTemplate = kafkaTemplate;
    }

    @Transactional
    public void relay() {
        List<T> pending = outboxEventRepository.findTop100ByPublishedAtIsNullOrderByCreatedAtAsc();
        for (T event : pending) {
            try {
                kafkaTemplate.send(event.getTopic(), event.getAggregateId(), event.getPayload()).get();
                event.setPublishedAt(LocalDateTime.now());
                outboxEventRepository.save(event);
            } catch (Exception ex) {
                log.warn("Failed to publish outbox event id={} topic={}, will retry next cycle: {}",
                        event.getId(), event.getTopic(), ex.getMessage());
            }
        }
    }

    /**
     * 가장 오래된 행부터 id 순으로 한 묶음만 들여다보고, 그중 보존 기간이 지난 발행분만
     * 지운다. PK 만 타므로 테이블이 얼마나 크든 한 주기 비용이 같다. 미발행 행은
     * published_at 이 null 이라 남는다. 서브클래스가 @Scheduled 로 감싸 호출한다.
     *
     * <p>실패를 삼키는 이유: 던지면 스케줄러가 매 주기 ERROR 스택을 찍는다. 정리는 다음
     * 주기에 다시 시도하면 되는 일이고, 로그 이상탐지에 주기적 스택을 얹을 이유가 없다.
     */
    public void purgePublished() {
        if (!purgeEnabled) {
            return;
        }
        try {
            LocalDateTime cutoff = LocalDateTime.now().minusHours(purgeRetentionHours);
            List<Long> ids = outboxEventRepository.findOldest(PageRequest.of(0, purgeBatchSize)).stream()
                    .filter(head -> head.getPublishedAt() != null && head.getPublishedAt().isBefore(cutoff))
                    .map(OutboxEventRepository.Head::getId)
                    .toList();
            if (!ids.isEmpty()) {
                outboxEventRepository.deleteAllByIdInBatch(ids);
                log.debug("Purged {} published outbox events older than {}h", ids.size(), purgeRetentionHours);
            }
        } catch (Exception ex) {
            log.warn("Outbox purge failed, will retry next cycle: {}", ex.getMessage());
        }
    }
}
