package com.fooddelivery.dispatch.repository;

import com.fooddelivery.common.outbox.OutboxEventRepository;
import com.fooddelivery.dispatch.entity.DispatchOutboxEvent;

public interface DispatchOutboxEventRepository extends OutboxEventRepository<DispatchOutboxEvent> {
}
