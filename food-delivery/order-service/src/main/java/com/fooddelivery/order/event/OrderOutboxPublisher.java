package com.fooddelivery.order.event;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fooddelivery.common.outbox.OutboxPublisher;
import com.fooddelivery.order.entity.OrderOutboxEvent;
import com.fooddelivery.order.repository.OrderOutboxEventRepository;
import org.springframework.stereotype.Component;

@Component
public class OrderOutboxPublisher extends OutboxPublisher<OrderOutboxEvent> {

    public OrderOutboxPublisher(OrderOutboxEventRepository repository, ObjectMapper objectMapper) {
        super(repository, objectMapper);
    }

    @Override
    protected OrderOutboxEvent newEvent() {
        return new OrderOutboxEvent();
    }
}
