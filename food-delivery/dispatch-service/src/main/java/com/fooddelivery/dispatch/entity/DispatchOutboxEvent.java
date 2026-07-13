package com.fooddelivery.dispatch.entity;

import com.fooddelivery.common.outbox.OutboxEvent;
import jakarta.persistence.Entity;
import jakarta.persistence.Table;

@Entity
@Table(name = "dispatch_outbox_events")
public class DispatchOutboxEvent extends OutboxEvent {
}
