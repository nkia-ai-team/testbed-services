package com.fooddelivery.payment.entity;

import com.fooddelivery.common.outbox.OutboxEvent;
import jakarta.persistence.Entity;
import jakarta.persistence.Table;

@Entity
@Table(name = "payment_outbox_events")
public class PaymentOutboxEvent extends OutboxEvent {
}
