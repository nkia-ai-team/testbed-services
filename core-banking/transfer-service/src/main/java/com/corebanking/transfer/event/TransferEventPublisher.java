package com.corebanking.transfer.event;

import com.corebanking.common.dto.TransferEvent;
import com.corebanking.common.outbox.OutboxPublisher;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 이체 완료/실패 이벤트를 outbox_events 에 기록한다(DB 트랜잭션과 원자적).
 * 실제 Kafka(banking.transfers) 발행은 OutboxRelay 가 별도 스케줄로 수행 —
 * 기존 Redis Streams(transfer-events) 대신 Kafka 이벤트 백본으로 대체(commerce outbox 패턴 정합).
 */
@Component
public class TransferEventPublisher {

    private static final Logger log = LoggerFactory.getLogger(TransferEventPublisher.class);

    private final OutboxPublisher outboxPublisher;
    private final String transfersTopic;

    public TransferEventPublisher(OutboxPublisher outboxPublisher,
                                   @Value("${topics.transfers:banking.transfers}") String transfersTopic) {
        this.outboxPublisher = outboxPublisher;
        this.transfersTopic = transfersTopic;
    }

    public void publish(TransferEvent event) {
        try {
            outboxPublisher.publish(transfersTopic, "TRANSFER", event.transferId(),
                    "TRANSFER_" + event.status(), event);
            log.info("Recorded outbox event for transfer: transferId={}, status={}",
                    event.transferId(), event.status());
        } catch (Exception ex) {
            log.error("Failed to record transfer outbox event (non-critical): transferId={}, error={}",
                    event.transferId(), ex.getMessage());
        }
    }
}
