package com.fooddelivery.common.dto;

import java.math.BigDecimal;

public record PaymentRequest(
        Long orderId,
        BigDecimal amount,
        String pgProvider
) {}
