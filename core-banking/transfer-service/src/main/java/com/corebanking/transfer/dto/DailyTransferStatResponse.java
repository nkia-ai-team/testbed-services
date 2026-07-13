package com.corebanking.transfer.dto;

import java.math.BigDecimal;
import java.time.LocalDate;

public record DailyTransferStatResponse(
        LocalDate date,
        long count,
        BigDecimal totalAmount
) {
}
