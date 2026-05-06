package com.fooddelivery.common.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record OrderResponse(
        Long id,
        String customerId,
        Long restaurantId,
        BigDecimal totalAmount,
        String status,
        LocalDateTime createdAt
) {}
