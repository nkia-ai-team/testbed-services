package com.plopvape.common.dto;

public record PaymentResponse(
        Long paymentId,
        String status,
        String pgTransactionId
) {}
