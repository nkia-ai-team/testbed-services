package com.commerce.common.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record PaymentSummaryResponse(
        Long paymentId,
        Long orderId,
        BigDecimal amount,
        String method,
        String status,
        String pgTransactionId,
        LocalDateTime settledAt,
        LocalDateTime createdAt
) {}
