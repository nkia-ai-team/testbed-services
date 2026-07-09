package com.plopvape.notification.consumer;

import com.plopvape.notification.service.NotificationService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.stream.StreamListener;
import org.springframework.stereotype.Component;

import java.util.Map;

@Component
public class OrderEventConsumer implements StreamListener<String, MapRecord<String, String, String>> {

    private static final Logger log = LoggerFactory.getLogger(OrderEventConsumer.class);
    private static final String STREAM_KEY = "order-events";
    private static final String GROUP_NAME = "notification-group";

    private final NotificationService notificationService;
    private final StringRedisTemplate redisTemplate;

    public OrderEventConsumer(NotificationService notificationService,
                              StringRedisTemplate redisTemplate) {
        this.notificationService = notificationService;
        this.redisTemplate = redisTemplate;
    }

    @Override
    public void onMessage(MapRecord<String, String, String> message) {
        Map<String, String> fields = message.getValue();
        log.info("Received order event: {}", fields);

        try {
            notificationService.sendNotification(
                    fields.get("orderId"),
                    fields.get("customerName"),
                    fields.get("customerEmail"),
                    fields.get("totalAmount"),
                    fields.get("status")
            );

            redisTemplate.opsForStream().acknowledge(STREAM_KEY, GROUP_NAME, message.getId());
            log.info("ACK message: {}", message.getId());
        } catch (Exception e) {
            log.error("Failed to process order event: {}", e.getMessage(), e);
        }
    }
}
