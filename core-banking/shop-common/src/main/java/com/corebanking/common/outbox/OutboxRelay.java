package com.corebanking.common.outbox;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;

/**
 * outbox_events 를 폴링해 Kafka 로 발행하는 릴레이. producer 서비스에서만
 * outbox.relay.enabled=true 로 활성화한다(consumer-only 서비스는 비활성).
 * at-least-once: 발행 실패 시 published_at 이 null 로 남아 다음 주기에 재시도.
 */
@Component
@ConditionalOnProperty(prefix = "outbox.relay", name = "enabled", havingValue = "true")
public class OutboxRelay {

    private static final Logger log = LoggerFactory.getLogger(OutboxRelay.class);

    private final OutboxEventRepository outboxEventRepository;
    private final KafkaTemplate<String, String> kafkaTemplate;
    private final OutboxRelayControlRepository controlRepository;
    private final String controlId;

    public OutboxRelay(OutboxEventRepository outboxEventRepository, KafkaTemplate<String, String> kafkaTemplate,
                       OutboxRelayControlRepository controlRepository,
                       @Value("${outbox.relay.control-id:}") String controlId) {
        this.outboxEventRepository = outboxEventRepository;
        this.kafkaTemplate = kafkaTemplate;
        this.controlRepository = controlRepository;
        this.controlId = controlId;
    }

    @Scheduled(fixedDelayString = "${outbox.relay.poll-interval-ms:2000}")
    @Transactional
    public void relay() {
        // outbox_relay_control 행이 재기동 없는 정지 스위치다(OutboxRelayControl 참조).
        // 행 부재·조회 실패는 정지 근거가 아니므로 fail-open — 릴레이가 스위치보다 중요하다.
        if (!controlId.isEmpty()) {
            try {
                boolean enabled = controlRepository.findById(controlId)
                        .map(OutboxRelayControl::isEnabled)
                        .orElse(true);
                if (!enabled) {
                    return;
                }
            } catch (Exception ex) {
                log.warn("Outbox relay control lookup failed (relay continues): {}", ex.getMessage());
            }
        }
        List<OutboxEvent> pending = outboxEventRepository.findTop100ByPublishedAtIsNullOrderByCreatedAtAsc();
        for (OutboxEvent event : pending) {
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
