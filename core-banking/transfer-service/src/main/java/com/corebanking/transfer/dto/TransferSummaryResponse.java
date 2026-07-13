package com.corebanking.transfer.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record TransferSummaryResponse(
        Long id,
        String transferRef,
        String fromAccount,
        String toAccount,
        BigDecimal amount,
        String status,
        String orderId,
        LocalDateTime createdAt
) {
}
