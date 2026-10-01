package com.corebanking.transfer.batch;

import com.corebanking.transfer.service.TransferService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 보존 기간이 지난 이체를 조금씩 지운다. transfers 는 이체마다 한 행이 쌓이기만 해서
 * (하루 약 8만~10만 건) 그대로 두면 Oracle Free 의 DB 크기 상한(12GB, ORA-12954)에 다시
 * 닿는다 — 2026-09-29 에 실제로 닿아 이체 기록이 37시간 멎었다. 90일 보존이면 이체와
 * 원장을 합쳐 약 7GB 에서 멈춘다. 90일은 시드가 만든 이력 범위이자 일별 통계 조회의
 * 최대 범위다.
 *
 * <p>한 번에 몰아 지우지 않고 주기마다 작은 묶음만 지운다. 큰 삭제는 그 자체가 부하
 * 이상으로 관측되고 정상 구간 녹화를 흐린다.
 */
@Component
public class TransferRetentionBatch {

    private static final Logger log = LoggerFactory.getLogger(TransferRetentionBatch.class);

    private final TransferService transferService;
    private final boolean enabled;
    private final int retentionDays;
    private final int batchSize;

    public TransferRetentionBatch(TransferService transferService,
                                  @Value("${transfer.retention.enabled:true}") boolean enabled,
                                  @Value("${transfer.retention.days:90}") int retentionDays,
                                  @Value("${transfer.retention.batch-size:500}") int batchSize) {
        this.transferService = transferService;
        this.enabled = enabled;
        this.retentionDays = retentionDays;
        this.batchSize = batchSize;
    }

    // 실패를 삼키는 이유: 던지면 스케줄러가 매 주기 ERROR 스택을 찍는다. 정리는 다음 주기에
    // 다시 시도하면 되는 일이고, 로그 이상탐지에 주기적 스택을 얹을 이유가 없다.
    @Scheduled(fixedDelayString = "${transfer.retention.interval-ms:60000}")
    public void purge() {
        if (!enabled) {
            return;
        }
        try {
            int purged = transferService.purgeOlderThan(retentionDays, batchSize);
            if (purged > 0) {
                log.debug("Purged {} transfers older than {} days", purged, retentionDays);
            }
        } catch (Exception ex) {
            log.warn("Transfer retention purge failed, will retry next cycle: {}", ex.getMessage());
        }
    }
}
