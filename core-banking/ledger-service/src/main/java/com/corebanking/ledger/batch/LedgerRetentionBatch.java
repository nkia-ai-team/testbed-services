package com.corebanking.ledger.batch;

import com.corebanking.ledger.service.LedgerService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 보존 기간이 지난 원장 줄을 조금씩 지운다. ledger_entries 는 완료된 이체마다 두 줄이
 * 쌓이기만 해서 그대로 두면 Oracle Free 의 DB 크기 상한(12GB, ORA-12954)에 다시 닿는다 —
 * 2026-09-29 에 실제로 닿았다. 대사 배치가 매번 전체 줄의 합을 구하므로 줄 수를 묶어 두는
 * 것은 그 배치의 비용을 묶는 일이기도 하다.
 *
 * <p>기간은 transfer-service 의 transfer.retention.days 와 같은 값이어야 한다. 한 번에 몰아
 * 지우지 않고 주기마다 작은 묶음만 지운다 — 큰 삭제는 그 자체가 부하 이상으로 관측된다.
 */
@Component
public class LedgerRetentionBatch {

    private static final Logger log = LoggerFactory.getLogger(LedgerRetentionBatch.class);

    private final LedgerService ledgerService;
    private final boolean enabled;
    private final int retentionDays;
    private final int batchSize;

    public LedgerRetentionBatch(LedgerService ledgerService,
                                @Value("${ledger.retention.enabled:true}") boolean enabled,
                                @Value("${ledger.retention.days:90}") int retentionDays,
                                @Value("${ledger.retention.batch-size:500}") int batchSize) {
        this.ledgerService = ledgerService;
        this.enabled = enabled;
        this.retentionDays = retentionDays;
        this.batchSize = batchSize;
    }

    // 실패를 삼키는 이유: 던지면 스케줄러가 매 주기 ERROR 스택을 찍는다. F14-P 는 이 테이블을
    // 읽기 전용으로 바꾸므로 주입 중에는 삭제가 실패하는 것이 정상이고, 다음 주기에 다시 시도한다.
    @Scheduled(fixedDelayString = "${ledger.retention.interval-ms:60000}")
    public void purge() {
        if (!enabled) {
            return;
        }
        try {
            int purged = ledgerService.purgeOlderThan(retentionDays, batchSize);
            if (purged > 0) {
                log.debug("Purged {} ledger entries older than {} days", purged, retentionDays);
            }
        } catch (Exception ex) {
            log.warn("Ledger retention purge failed, will retry next cycle: {}", ex.getMessage());
        }
    }
}
