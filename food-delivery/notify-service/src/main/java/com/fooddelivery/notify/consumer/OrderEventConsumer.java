package com.fooddelivery.notify.consumer;

import com.fooddelivery.notify.service.NotifyService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

/**
 * food.orders 구독(구 Redis Streams → Kafka 전환). order-service의 outbox 릴레이가
 * 발행한 OrderEvent(JSON) payload 를 그대로 로그로 넘긴다 — notify-service는 실제 발송
 * 없이 시뮬레이션만 하므로 파싱 없이 원문 그대로 사용해도 충분하다.
 */
@Component
public class OrderEventConsumer {

    private static final Logger log = LoggerFactory.getLogger(OrderEventConsumer.class);

    private final NotifyService notifyService;

    public OrderEventConsumer(NotifyService notifyService) {
        this.notifyService = notifyService;
    }

    @KafkaListener(topics = "${topics.orders}", groupId = "notify-service")
    public void onOrderEvent(String message) {
        log.info("Received order event: {}", message);
        try {
            notifyService.sendNotification("order", message);
        } catch (Exception ex) {
            log.error("Failed to process order event: {}", ex.getMessage(), ex);
        }
    }
}
