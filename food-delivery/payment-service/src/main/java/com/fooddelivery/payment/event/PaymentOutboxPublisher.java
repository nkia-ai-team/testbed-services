package com.fooddelivery.payment.event;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fooddelivery.common.outbox.OutboxPublisher;
import com.fooddelivery.payment.entity.PaymentOutboxEvent;
import com.fooddelivery.payment.repository.PaymentOutboxEventRepository;
import org.springframework.stereotype.Component;

@Component
public class PaymentOutboxPublisher extends OutboxPublisher<PaymentOutboxEvent> {

    public PaymentOutboxPublisher(PaymentOutboxEventRepository repository, ObjectMapper objectMapper) {
        super(repository, objectMapper);
    }

    @Override
    protected PaymentOutboxEvent newEvent() {
        return new PaymentOutboxEvent();
    }
}
