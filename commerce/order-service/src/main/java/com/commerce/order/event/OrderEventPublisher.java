package com.commerce.order.event;

import com.commerce.common.dto.OrderEvent;
import com.commerce.common.outbox.OutboxPublisher;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public class OrderEventPublisher {

    private static final Logger log = LoggerFactory.getLogger(OrderEventPublisher.class);

    private final OutboxPublisher outboxPublisher;
    private final String ordersTopic;

    public OrderEventPublisher(OutboxPublisher outboxPublisher,
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
