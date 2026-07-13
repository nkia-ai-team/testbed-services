package com.commerce.common.outbox;

import com.commerce.common.exception.ServiceException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

/**
 * 비즈니스 트랜잭션 안에서 호출하는 outbox insert 헬퍼.
 * 같은 트랜잭션으로 커밋되므로 비즈니스 상태 변경과 이벤트 기록이 원자적이다.
 * 실제 Kafka 발행은 OutboxRelay가 별도 스케줄러로 폴링해 처리한다.
 */
@Component
public class OutboxPublisher {

    private final OutboxEventRepository outboxEventRepository;
    private final ObjectMapper objectMapper;

    public OutboxPublisher(OutboxEventRepository outboxEventRepository, ObjectMapper objectMapper) {
        this.outboxEventRepository = outboxEventRepository;
        this.objectMapper = objectMapper;
    }

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
