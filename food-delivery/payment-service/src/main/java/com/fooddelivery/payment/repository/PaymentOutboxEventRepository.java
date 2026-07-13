package com.fooddelivery.payment.repository;

import com.fooddelivery.common.outbox.OutboxEventRepository;
import com.fooddelivery.payment.entity.PaymentOutboxEvent;

public interface PaymentOutboxEventRepository extends OutboxEventRepository<PaymentOutboxEvent> {
}
