package com.commerce.notification.consumer;

import com.commerce.common.dto.OrderEvent;
import com.commerce.notification.service.NotificationService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class OrderEventConsumer {

    private static final Logger log = LoggerFactory.getLogger(OrderEventConsumer.class);

    private final NotificationService notificationService;
    private final ObjectMapper objectMapper;

    public OrderEventConsumer(NotificationService notificationService, ObjectMapper objectMapper) {
        this.notificationService = notificationService;
        this.objectMapper = objectMapper;
    }

    @KafkaListener(topics = "${topics.orders}", groupId = "notification-service")
    public void onOrderEvent(String message) {
        log.info("Received order event: {}", message);
        try {
            OrderEvent event = objectMapper.readValue(message, OrderEvent.class);
            notificationService.sendNotification(
                    String.valueOf(event.orderId()),
                    event.customerName(),
                    event.customerEmail(),
                    event.totalAmount() != null ? event.totalAmount().toString() : null,
                    event.status());
        } catch (Exception ex) {
            log.error("Failed to process order event: {}", ex.getMessage(), ex);
        }
    }
}
