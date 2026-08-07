package com.corebanking.common.outbox;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.SendResult;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/**
 * outbox_events 를 폴링해 Kafka 로 발행하는 릴레이. producer 서비스에서만
 * outbox.relay.enabled=true 로 활성화한다(consumer-only 서비스는 비활성).
 * at-least-once: 발행 실패 시 published_at 이 null 로 남아 다음 주기에 재시도.
 *
 * <p>DB 트랜잭션과 Kafka 발행은 분리돼 있다. 한 주기는 (1) 짧은 읽기 트랜잭션으로
 * pending 조회 (2) 트랜잭션 밖에서 발행 (3) 성공분만 짧은 쓰기 트랜잭션으로 배치
 * 마킹, 세 구간이다. 발행을 트랜잭션 안에 두면 브로커가 느려질 때 릴레이가 Hikari
 * 커넥션을 브로커 응답 시간만큼 쥐고, 배치 크기(최대 100)만큼 그것이 곱해진다 —
 * 풀이 마르면 이체 경로와 DataSource 헬스 인디케이터가 같은 풀에서 함께 굶는다.
 */
@Component
@ConditionalOnProperty(prefix = "outbox.relay", name = "enabled", havingValue = "true")
public class OutboxRelay {

    private static final Logger log = LoggerFactory.getLogger(OutboxRelay.class);

    private final OutboxRelayStore store;
    private final KafkaTemplate<String, String> kafkaTemplate;
    private final String controlId;
    private final long publishTimeoutMs;

    public OutboxRelay(OutboxRelayStore store, KafkaTemplate<String, String> kafkaTemplate,
                       @Value("${outbox.relay.control-id:}") String controlId,
                       @Value("${outbox.relay.publish-timeout-ms:10000}") long publishTimeoutMs) {
        this.store = store;
        this.kafkaTemplate = kafkaTemplate;
        this.controlId = controlId;
        this.publishTimeoutMs = publishTimeoutMs;
    }

    /**
     * 트랜잭션 애노테이션이 없어야 한다. 여기에 @Transactional 이 붙으면 아래 발행
     * 구간이 다시 트랜잭션 안으로 들어와 커넥션 점유 결함이 그대로 되살아난다
     * (OutboxRelayTest 가 이 부재를 고정한다).
     */
    @Scheduled(fixedDelayString = "${outbox.relay.poll-interval-ms:2000}")
    public void relay() {
        if (!relayEnabled()) {
            return;
        }
        List<OutboxEvent> pending = store.findPending();
        if (pending.isEmpty()) {
            return;
        }
        List<Long> published = publish(pending);
        if (!published.isEmpty()) {
            store.markPublished(published, LocalDateTime.now());
        }
    }

    // outbox_relay_control 행이 재기동 없는 정지 스위치다(OutboxRelayControl 참조).
    // 행 부재·조회 실패는 정지 근거가 아니므로 fail-open — 릴레이가 스위치보다 중요하다.
    private boolean relayEnabled() {
        if (controlId.isEmpty()) {
            return true;
        }
        try {
            return store.findControl(controlId)
                    .map(OutboxRelayControl::isEnabled)
                    .orElse(true);
        } catch (Exception ex) {
            log.warn("Outbox relay control lookup failed (relay continues): {}", ex.getMessage());
            return true;
        }
    }

    /**
     * 배치 전체를 먼저 producer 에 넘긴 뒤 한 번만 기다린다. send() 는 이미 비동기라
     * 브로커 왕복이 건당 직렬화되지 않고, 대기는 배치 전체에 대해 한 번 유계로 걸린다.
     * 시한 안에 성공한 것만 돌려준다 — 미완료·실패분은 published_at 이 null 로 남아
     * 다음 주기에 재시도된다(at-least-once).
     */
    private List<Long> publish(List<OutboxEvent> pending) {
        Map<Long, CompletableFuture<SendResult<String, String>>> inFlight = new LinkedHashMap<>();
        for (OutboxEvent event : pending) {
            try {
                inFlight.put(event.getId(),
                        kafkaTemplate.send(event.getTopic(), event.getAggregateId(), event.getPayload()));
            } catch (Exception ex) {
                log.warn("Failed to hand outbox event id={} topic={} to the producer, will retry next cycle: {}",
                        event.getId(), event.getTopic(), ex.getMessage());
            }
        }
        try {
            CompletableFuture.allOf(inFlight.values().toArray(new CompletableFuture[0]))
                    .get(publishTimeoutMs, TimeUnit.MILLISECONDS);
        } catch (Exception ex) {
            // 배치 일부가 시한 안에 안 끝났거나 실패했다. 개별 결과는 아래에서 가른다.
            log.warn("Outbox publish batch of {} did not fully settle within {}ms: {}",
                    inFlight.size(), publishTimeoutMs, ex.getMessage());
        }
        List<Long> published = new ArrayList<>();
        for (Map.Entry<Long, CompletableFuture<SendResult<String, String>>> entry : inFlight.entrySet()) {
            CompletableFuture<SendResult<String, String>> future = entry.getValue();
            if (future.isDone() && !future.isCompletedExceptionally()) {
                published.add(entry.getKey());
            } else {
                log.warn("Outbox event id={} not confirmed published, will retry next cycle", entry.getKey());
            }
        }
        return published;
    }
}
