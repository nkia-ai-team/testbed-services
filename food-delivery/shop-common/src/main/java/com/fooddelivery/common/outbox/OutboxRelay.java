package com.fooddelivery.common.outbox;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
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
 */
public abstract class OutboxRelay<T extends OutboxEvent> {

    private static final Logger log = LoggerFactory.getLogger(OutboxRelay.class);

    private final OutboxEventRepository<T> outboxEventRepository;
    private final KafkaTemplate<String, String> kafkaTemplate;

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
}
