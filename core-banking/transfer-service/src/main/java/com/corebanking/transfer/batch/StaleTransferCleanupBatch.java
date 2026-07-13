package com.corebanking.transfer.batch;

import com.corebanking.transfer.service.TransferService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 오래도록 PENDING 상태로 남은 이체를 FAILED 로 정리한다.
 * 정상 흐름에서는 execute() 가 동기적으로 상태를 확정하므로 이 배치가 처리하는 건은
 * 비정상 종료(예외로 커밋 실패) 케이스에 한정된다.
 */
@Component
public class StaleTransferCleanupBatch {

    private static final Logger log = LoggerFactory.getLogger(StaleTransferCleanupBatch.class);

    private final TransferService transferService;
    private final int staleAfterMinutes;

    public StaleTransferCleanupBatch(TransferService transferService,
                                      @Value("${transfer.cleanup.stale-after-minutes:30}") int staleAfterMinutes) {
        this.transferService = transferService;
        this.staleAfterMinutes = staleAfterMinutes;
    }

    @Scheduled(fixedDelayString = "${transfer.cleanup.interval-ms:600000}")
    public void cleanup() {
        log.info("Stale transfer cleanup batch started: staleAfterMinutes={}", staleAfterMinutes);
        int cleaned = transferService.cleanupStalePending(staleAfterMinutes);
        log.info("Stale transfer cleanup batch finished: cleaned={}", cleaned);
    }
}
