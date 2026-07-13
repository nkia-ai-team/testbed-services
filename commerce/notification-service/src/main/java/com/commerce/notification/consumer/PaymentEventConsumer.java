package com.commerce.notification.consumer;

import com.commerce.common.dto.PaymentEvent;
import com.commerce.notification.service.NotificationService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class PaymentEventConsumer {

    private static final Logger log = LoggerFactory.getLogger(PaymentEventConsumer.class);

    private final NotificationService notificationService;
    private final ObjectMapper objectMapper;

    public PaymentEventConsumer(NotificationService notificationService, ObjectMapper objectMapper) {
        this.notificationService = notificationService;
        this.objectMapper = objectMapper;
    }

    @KafkaListener(topics = "${topics.payments}", groupId = "notification-service")
    public void onPaymentEvent(String message) {
        log.info("Received payment event: {}", message);
        try {
            PaymentEvent event = objectMapper.readValue(message, PaymentEvent.class);
            notificationService.sendPaymentNotification(
                    String.valueOf(event.paymentId()),
                    String.valueOf(event.orderId()),
                    event.status());
        } catch (Exception ex) {
            log.error("Failed to process payment event: {}", ex.getMessage(), ex);
        }
    }
}
