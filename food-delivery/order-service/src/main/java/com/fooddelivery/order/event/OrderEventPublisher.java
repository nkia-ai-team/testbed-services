package com.fooddelivery.order.event;

import com.fooddelivery.common.dto.OrderEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * Redis Streams(구) → Kafka + outbox(신규)로 전환. 비즈니스 트랜잭션과 같은 커밋에 outbox
 * 행을 남기고, 실제 Kafka 발행은 OrderOutboxRelay가 별도 폴링으로 처리한다(at-least-once).
 */
@Component
public class OrderEventPublisher {

    private static final Logger log = LoggerFactory.getLogger(OrderEventPublisher.class);

    private final OrderOutboxPublisher outboxPublisher;
    private final String ordersTopic;

    public OrderEventPublisher(OrderOutboxPublisher outboxPublisher,
                                @Value("${topics.orders}") String ordersTopic) {
        this.outboxPublisher = outboxPublisher;
        this.ordersTopic = ordersTopic;
    }

    public void publish(OrderEvent event) {
        outboxPublisher.publish(ordersTopic, "ORDER", String.valueOf(event.orderId()),
                "ORDER_" + event.status(), event);
        log.info("Recorded outbox event for order: orderId={}, status={}", event.orderId(), event.status());
    }
}
