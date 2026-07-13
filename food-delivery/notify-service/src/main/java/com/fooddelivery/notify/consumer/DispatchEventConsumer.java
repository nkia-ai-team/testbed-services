package com.fooddelivery.notify.consumer;

import com.fooddelivery.notify.service.NotifyService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class DispatchEventConsumer {

    private static final Logger log = LoggerFactory.getLogger(DispatchEventConsumer.class);

    private final NotifyService notifyService;

    public DispatchEventConsumer(NotifyService notifyService) {
        this.notifyService = notifyService;
    }

    @KafkaListener(topics = "${topics.dispatch}", groupId = "notify-service")
    public void onDispatchEvent(String message) {
        log.info("Received dispatch event: {}", message);
        try {
            notifyService.sendNotification("dispatch", message);
        } catch (Exception ex) {
            log.error("Failed to process dispatch event: {}", ex.getMessage(), ex);
        }
    }
}
