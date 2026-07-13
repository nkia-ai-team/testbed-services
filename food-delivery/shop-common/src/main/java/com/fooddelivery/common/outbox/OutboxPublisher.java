package com.fooddelivery.common.outbox;

import com.fooddelivery.common.exception.ServiceException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.http.HttpStatus;

/**
 * 비즈니스 트랜잭션 안에서 호출하는 outbox insert 헬퍼. 같은 트랜잭션으로 커밋되므로
 * 비즈니스 상태 변경과 이벤트 기록이 원자적이다. 실제 Kafka 발행은 OutboxRelay가 별도
 * 스케줄러로 폴링해 처리한다. 서비스별 구체 엔티티(T)를 만들어야 해서 newEvent()를
 * 서브클래스가 구현한다(shop-common은 도메인 엔티티를 모르므로).
 */
public abstract class OutboxPublisher<T extends OutboxEvent> {

    private final OutboxEventRepository<T> outboxEventRepository;
    private final ObjectMapper objectMapper;

    protected OutboxPublisher(OutboxEventRepository<T> outboxEventRepository, ObjectMapper objectMapper) {
        this.outboxEventRepository = outboxEventRepository;
        this.objectMapper = objectMapper;
    }

    protected abstract T newEvent();

    public void publish(String topic, String aggregateType, String aggregateId, String eventType, Object payload) {
        try {
            T event = newEvent();
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
