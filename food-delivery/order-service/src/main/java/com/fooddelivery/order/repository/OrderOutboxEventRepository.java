package com.fooddelivery.order.repository;

import com.fooddelivery.common.outbox.OutboxEventRepository;
import com.fooddelivery.order.entity.OrderOutboxEvent;

public interface OrderOutboxEventRepository extends OutboxEventRepository<OrderOutboxEvent> {
}
