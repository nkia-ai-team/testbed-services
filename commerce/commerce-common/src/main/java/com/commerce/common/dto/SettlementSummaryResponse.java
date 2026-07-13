package com.commerce.common.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record SettlementSummaryResponse(
        Long id,
        LocalDateTime periodStart,
        LocalDateTime periodEnd,
        int paymentCount,
        BigDecimal totalAmount,
        String bankingTransferStatus,
        String bankingTransferRef,
        LocalDateTime createdAt
) {}
