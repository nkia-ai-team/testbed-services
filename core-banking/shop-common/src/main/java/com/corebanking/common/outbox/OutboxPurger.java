package com.corebanking.common.outbox;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;

/**
 * 발행이 끝난 outbox 행을 보존 기간 뒤에 지운다. 릴레이는 published_at 만 찍고 행을
 * 남기는데 발행된 행을 다시 읽는 곳은 없다. 지우지 않으면 테이블이 유입량만큼 끝없이
 * 자란다 — 2026-09-29 Oracle Free 의 DB 크기 상한(12GB, ORA-12954)에 닿아 이체 기록이
 * 37시간 멎었고, 그때 BANKING 스키마의 58%(1,076만 행, 전부 발행 완료)가 이 테이블이었다.
 *
 * <p>릴레이가 켜진 서비스에서만 돈다. banking 은 transfer·ledger 가 한 테이블을 공유하고
 * 릴레이는 transfer 하나만 켜 두므로(ledger application.yml 참조) 정리도 한 곳에서만 돈다.
 * 미발행 행은 지우지 않는다 — 릴레이 정지 시나리오(F18-P)의 적체는 그대로 남는다.
 */
@Component
@ConditionalOnProperty(prefix = "outbox.relay", name = "enabled", havingValue = "true")
public class OutboxPurger {

    private static final Logger log = LoggerFactory.getLogger(OutboxPurger.class);

    private final OutboxRelayStore store;
    private final boolean enabled;
    private final long retentionHours;
    private final int batchSize;

    public OutboxPurger(OutboxRelayStore store,
                        @Value("${outbox.purge.enabled:true}") boolean enabled,
                        @Value("${outbox.purge.retention-hours:24}") long retentionHours,
                        @Value("${outbox.purge.batch-size:500}") int batchSize) {
        this.store = store;
        this.enabled = enabled;
        this.retentionHours = retentionHours;
        this.batchSize = batchSize;
    }

    // 실패를 삼키는 이유: 던지면 스케줄러가 매 주기 ERROR 스택을 찍는다. 정리는 다음 주기에
    // 다시 시도하면 되는 일이고, 로그 이상탐지에 주기적 스택을 얹을 이유가 없다.
    @Scheduled(fixedDelayString = "${outbox.purge.interval-ms:30000}")
    public void purge() {
        if (!enabled) {
            return;
        }
        try {
            int purged = store.purgePublishedBefore(LocalDateTime.now().minusHours(retentionHours), batchSize);
            if (purged > 0) {
                log.debug("Purged {} published outbox events older than {}h", purged, retentionHours);
            }
        } catch (Exception ex) {
            log.warn("Outbox purge failed, will retry next cycle: {}", ex.getMessage());
        }
    }
}
