package com.commerce.shipping.event;

import com.commerce.common.dto.ShippingEvent;
import com.commerce.common.outbox.OutboxPublisher;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public class ShippingEventPublisher {

    private static final Logger log = LoggerFactory.getLogger(ShippingEventPublisher.class);

    private final OutboxPublisher outboxPublisher;
    private final String shippingTopic;

    public ShippingEventPublisher(OutboxPublisher outboxPublisher,
                                   @Value("${topics.shipping}") String shippingTopic) {
        this.outboxPublisher = outboxPublisher;
        this.shippingTopic = shippingTopic;
    }

    public void publish(ShippingEvent event) {
        outboxPublisher.publish(shippingTopic, "SHIPMENT", String.valueOf(event.shipmentId()),
                "SHIPMENT_" + event.status(), event);
        log.info("Recorded outbox event for shipment: shipmentId={}, status={}", event.shipmentId(), event.status());
    }
}
