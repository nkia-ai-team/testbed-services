package com.corebanking.ledger.event;

import com.corebanking.common.outbox.OutboxPublisher;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.util.Map;

/**
 * 원장 반영 완료 이벤트를 banking.ledger 로 발행한다(outbox 패턴).
 * 현재 core-banking 내부에 이 토픽의 consumer 는 없음 — cross-domain 분석/알림 등
 * 향후 소비자를 위한 확장 지점(commerce 의 analytics 전용 토픽과 동일한 성격).
 */
@Component
public class LedgerEventPublisher {

    private static final Logger log = LoggerFactory.getLogger(LedgerEventPublisher.class);

    private final OutboxPublisher outboxPublisher;
    private final String ledgerTopic;

    public LedgerEventPublisher(OutboxPublisher outboxPublisher,
                                 @Value("${topics.ledger:banking.ledger}") String ledgerTopic) {
        this.outboxPublisher = outboxPublisher;
        this.ledgerTopic = ledgerTopic;
    }

    public void publishRecorded(String transferRef, String fromAccount, String toAccount, BigDecimal amount) {
        try {
            Map<String, Object> payload = Map.of(
                    "transferRef", transferRef,
                    "fromAccount", fromAccount,
                    "toAccount", toAccount,
                    "amount", amount
            );
            outboxPublisher.publish(ledgerTopic, "LEDGER", transferRef, "LEDGER_RECORDED", payload);
            log.info("Recorded outbox event for ledger: transferRef={}", transferRef);
        } catch (Exception ex) {
            log.error("Failed to record ledger outbox event (non-critical): transferRef={}, error={}",
                    transferRef, ex.getMessage());
        }
    }
}
