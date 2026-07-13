package com.fooddelivery.order.entity;

import com.fooddelivery.common.outbox.OutboxEvent;
import jakarta.persistence.Entity;
import jakarta.persistence.Table;

@Entity
@Table(name = "order_outbox_events")
public class OrderOutboxEvent extends OutboxEvent {
}
