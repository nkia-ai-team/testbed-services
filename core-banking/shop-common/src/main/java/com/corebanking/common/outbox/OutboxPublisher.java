package com.corebanking.common.outbox;

import com.corebanking.common.exception.ServiceException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

@Component
public class OutboxPublisher {

    private final OutboxEventRepository outboxEventRepository;
    private final ObjectMapper objectMapper;

    public OutboxPublisher(OutboxEventRepository outboxEventRepository, ObjectMapper objectMapper) {
        this.outboxEventRepository = outboxEventRepository;
        this.objectMapper = objectMapper;
    }

    /**
     * outbox_events 에 이벤트를 기록한다(비즈니스 트랜잭션과 동일 트랜잭션 내 호출 전제).
     * 실제 Kafka 발행은 OutboxRelay 가 별도 스케줄로 수행 — DB 커밋과 발행을 원자화(outbox 패턴).
     */
    public void publish(String topic, String aggregateType, String aggregateId, String eventType, Object payload) {
        try {
            OutboxEvent event = new OutboxEvent();
            event.setTopic(topic);
            event.setAggregateType(aggregateType);
            event.setAggregateId(aggregateId);
            event.setEventType(eventType);
            event.setPayload(objectMapper.writeValueAsString(payload));
            outboxEventRepository.save(event);
        } catch (Exception ex) {
            throw new ServiceException(HttpStatus.INTERNAL_SERVER_ERROR,
                    "Failed to record outbox event: " + ex.getMessage());
        }
    }
}
