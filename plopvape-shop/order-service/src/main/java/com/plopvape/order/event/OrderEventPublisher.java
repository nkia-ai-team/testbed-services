package com.plopvape.order.event;

import com.plopvape.common.dto.OrderEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.Map;

@Component
public class OrderEventPublisher {

    private static final Logger log = LoggerFactory.getLogger(OrderEventPublisher.class);
    private static final String STREAM_KEY = "order-events";

    private final StringRedisTemplate redisTemplate;

    public OrderEventPublisher(StringRedisTemplate redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    public void publish(OrderEvent event) {
        Map<String, String> fields = Map.of(
                "orderId", String.valueOf(event.orderId()),
                "customerName", event.customerName(),
                "customerEmail", event.customerEmail() != null ? event.customerEmail() : "",
                "totalAmount", event.totalAmount().toString(),
                "status", event.status(),
                "timestamp", Instant.now().toString()
        );
        redisTemplate.opsForStream().add(STREAM_KEY, fields);
        log.info("Published order event to Redis Streams: orderId={}", event.orderId());
    }
}
