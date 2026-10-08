package com.fooddelivery.dispatch.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 보존 기간(기본 30일)이 지난 끝난 배차와 전이 이력을 조금씩 지운다. dispatches·dispatch_events 는
 * 쌓이기만 해서 2026-10-08 추정 334만·563만 행이었고, 이 표를 훑는 조회가 커질수록 느려진다.
 *
 * <p>한 주기에 한 묶음(batch-size)만 지우고 다음 주기까지 쉰다. 묶음 사이를 스레드 안에서 재우지
 * 않는 이유: 스케줄러 스레드가 하나라 그동안 outbox 릴레이(2초)와 만료 배치가 멈춘다. 분당 상한은
 * batch-size × 60000 / interval-ms 로 정해진다(기본 2,000 × 10 = 2만 건).
 */
@Component
public class DispatchRetentionBatch {

    private static final Logger log = LoggerFactory.getLogger(DispatchRetentionBatch.class);

    private final DispatchRetentionService retentionService;
    private final boolean enabled;
    private final int retentionDays;
    private final int batchSize;
    // 다음 묶음의 시작점. 스케줄러 스레드 하나만 읽고 쓴다. 재기동하면 처음부터 다시 본다.
    private long resumeAfterId;

    public DispatchRetentionBatch(DispatchRetentionService retentionService,
                                  @Value("${dispatch.retention.enabled:true}") boolean enabled,
                                  @Value("${dispatch.retention.days:30}") int retentionDays,
                                  @Value("${dispatch.retention.batch-size:2000}") int batchSize) {
        this.retentionService = retentionService;
        this.enabled = enabled;
        this.retentionDays = retentionDays;
        this.batchSize = batchSize;
    }

    // 실패를 삼키는 이유: 던지면 스케줄러가 매 주기 ERROR 스택을 찍는다. 정리는 다음 주기에
    // 다시 시도하면 되는 일이고, 로그 이상탐지에 주기적 스택을 얹을 이유가 없다. 커서는 그대로 둔다.
    @Scheduled(fixedDelayString = "${dispatch.retention.interval-ms:6000}",
            initialDelayString = "${dispatch.retention.interval-ms:6000}")
    public void purge() {
        if (!enabled) {
            return;
        }
        try {
            DispatchRetentionService.Result result =
                    retentionService.purgeOlderThan(retentionDays, resumeAfterId, batchSize);
            resumeAfterId = result.resumeAfterId();
            if (result.purged() > 0) {
                log.debug("Purged {} dispatches and {} dispatch events older than {} days",
                        result.purged(), result.purgedEvents(), retentionDays);
            }
        } catch (Exception ex) {
            log.warn("Dispatch retention purge failed, will retry next cycle: {}", ex.getMessage());
        }
    }

    long resumeAfterId() {
        return resumeAfterId;
    }
}
