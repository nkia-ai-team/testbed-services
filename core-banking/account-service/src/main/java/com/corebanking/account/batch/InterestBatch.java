package com.corebanking.account.batch;

import com.corebanking.account.service.AccountService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 일할 이자 계산/기표 배치. 연이율(annual-rate-percent)을 365 로 나눈 일할 비율을
 * ACTIVE 계좌 잔액에 가산한다.
 */
@Component
public class InterestBatch {

    private static final Logger log = LoggerFactory.getLogger(InterestBatch.class);

    private final AccountService accountService;
    private final BigDecimal dailyRate;

    public InterestBatch(AccountService accountService,
                          @Value("${account.interest.annual-rate-percent:1.2}") BigDecimal annualRatePercent) {
        this.accountService = accountService;
        this.dailyRate = annualRatePercent
                .divide(BigDecimal.valueOf(100), 10, RoundingMode.HALF_UP)
                .divide(BigDecimal.valueOf(365), 10, RoundingMode.HALF_UP);
    }

    @Scheduled(cron = "${account.interest.cron:0 0 0 * * *}")
    public void applyInterest() {
        log.info("Interest accrual batch started: dailyRate={}", dailyRate);
        int applied = accountService.applyDailyInterest(dailyRate);
        log.info("Interest accrual batch finished: accountsUpdated={}", applied);
    }
}
