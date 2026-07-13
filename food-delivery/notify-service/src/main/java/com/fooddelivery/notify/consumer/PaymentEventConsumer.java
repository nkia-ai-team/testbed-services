package com.fooddelivery.notify.consumer;

import com.fooddelivery.notify.service.NotifyService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class PaymentEventConsumer {

    private static final Logger log = LoggerFactory.getLogger(PaymentEventConsumer.class);

    private final NotifyService notifyService;

    public PaymentEventConsumer(NotifyService notifyService) {
        this.notifyService = notifyService;
    }

    @KafkaListener(topics = "${topics.payments}", groupId = "notify-service")
    public void onPaymentEvent(String message) {
        log.info("Received payment event: {}", message);
        try {
            notifyService.sendNotification("payment", message);
        } catch (Exception ex) {
            log.error("Failed to process payment event: {}", ex.getMessage(), ex);
        }
    }
}
