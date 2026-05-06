package com.fooddelivery.common.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record PaymentResponse(
        Long id,
        Long orderId,
        String pgProvider,
        BigDecimal amount,
        String status,
        LocalDateTime processedAt
) {}
