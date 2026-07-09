package com.corebanking.ledger.consumer;

import com.corebanking.ledger.service.LedgerService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.stream.StreamListener;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.util.Map;

@Component
public class TransferEventConsumer implements StreamListener<String, MapRecord<String, String, String>> {

    private static final Logger log = LoggerFactory.getLogger(TransferEventConsumer.class);
    private static final String STREAM_KEY = "transfer-events";
    private static final String GROUP_NAME = "ledger-group";

    private final LedgerService ledgerService;
    private final StringRedisTemplate redisTemplate;

    public TransferEventConsumer(LedgerService ledgerService, StringRedisTemplate redisTemplate) {
        this.ledgerService = ledgerService;
        this.redisTemplate = redisTemplate;
    }

    @Override
    public void onMessage(MapRecord<String, String, String> message) {
        Map<String, String> fields = message.getValue();
        log.info("Received transfer event: {}", fields);

        try {
            ledgerService.recordTransfer(
                    fields.get("transferId"),
                    fields.get("fromAccount"),
                    fields.get("toAccount"),
                    new BigDecimal(fields.get("amount"))
            );

            redisTemplate.opsForStream().acknowledge(STREAM_KEY, GROUP_NAME, message.getId());
            log.info("ACK message: {}", message.getId());
        } catch (Exception e) {
            log.error("Failed to process transfer event: {}", e.getMessage(), e);
        }
    }
}
