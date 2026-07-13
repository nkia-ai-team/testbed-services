package com.corebanking.ledger.consumer;

import com.corebanking.common.dto.TransferEvent;
import com.corebanking.ledger.service.LedgerService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

/**
 * banking.transfers 토픽 구독 — transfer-service 의 outbox relay 가 발행한 이체 완료/실패
 * 이벤트를 소비해 원장에 반영한다(기존 Redis Streams consumer group 을 대체).
 */
@Component
public class TransferEventConsumer {

    private static final Logger log = LoggerFactory.getLogger(TransferEventConsumer.class);

    private final LedgerService ledgerService;
    private final ObjectMapper objectMapper;

    public TransferEventConsumer(LedgerService ledgerService, ObjectMapper objectMapper) {
        this.ledgerService = ledgerService;
        this.objectMapper = objectMapper;
    }

    @KafkaListener(topics = "${topics.transfers:banking.transfers}", groupId = "ledger-service")
    public void onTransferEvent(String message) {
        log.info("Received transfer event: {}", message);
        try {
            TransferEvent event = objectMapper.readValue(message, TransferEvent.class);
            if (!"COMPLETED".equals(event.status())) {
                log.info("Skipping non-completed transfer event: transferId={}, status={}",
                        event.transferId(), event.status());
                return;
            }
            ledgerService.recordTransfer(event.transferId(), event.fromAccount(),
                    event.toAccount(), event.amount());
        } catch (Exception ex) {
            log.error("Failed to process transfer event: {}", ex.getMessage(), ex);
        }
    }
}
