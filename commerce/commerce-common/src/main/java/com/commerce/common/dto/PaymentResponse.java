package com.commerce.common.dto;

public record PaymentResponse(
        Long paymentId,
        String status,
        String pgTransactionId
) {}
