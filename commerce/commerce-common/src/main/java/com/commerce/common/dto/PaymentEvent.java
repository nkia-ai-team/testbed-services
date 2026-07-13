package com.commerce.common.dto;

import java.math.BigDecimal;

public record PaymentEvent(
        Long paymentId,
        Long orderId,
        BigDecimal amount,
        String status,
        String pgTransactionId
) {}
