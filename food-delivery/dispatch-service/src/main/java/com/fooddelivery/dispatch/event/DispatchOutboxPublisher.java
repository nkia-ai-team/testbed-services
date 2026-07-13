package com.fooddelivery.dispatch.event;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fooddelivery.common.outbox.OutboxPublisher;
import com.fooddelivery.dispatch.entity.DispatchOutboxEvent;
import com.fooddelivery.dispatch.repository.DispatchOutboxEventRepository;
import org.springframework.stereotype.Component;

@Component
public class DispatchOutboxPublisher extends OutboxPublisher<DispatchOutboxEvent> {

    public DispatchOutboxPublisher(DispatchOutboxEventRepository repository, ObjectMapper objectMapper) {
        super(repository, objectMapper);
    }

    @Override
    protected DispatchOutboxEvent newEvent() {
        return new DispatchOutboxEvent();
    }
}
