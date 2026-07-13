package com.corebanking.ledger.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record LedgerEntryResponse(
        Long id,
        String transferRef,
        String accountId,
        String direction,
        BigDecimal amount,
        LocalDateTime createdAt
) {
}
