package com.corebanking.transfer.event;

import com.corebanking.common.dto.TransferEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.Map;

@Component
public class TransferEventPublisher {

    private static final Logger log = LoggerFactory.getLogger(TransferEventPublisher.class);
    private static final String STREAM_KEY = "transfer-events";

    private final StringRedisTemplate redisTemplate;

    public TransferEventPublisher(StringRedisTemplate redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    public void publish(TransferEvent event) {
        Map<String, String> fields = Map.of(
                "transferId", event.transferId(),
                "fromAccount", event.fromAccount(),
                "toAccount", event.toAccount(),
                "amount", event.amount().toString(),
                "orderId", event.orderId() != null ? event.orderId() : "",
                "status", event.status(),
                "timestamp", Instant.now().toString()
        );
        redisTemplate.opsForStream().add(STREAM_KEY, fields);
        log.info("Published transfer event to Redis Streams: transferId={}", event.transferId());
    }
}
