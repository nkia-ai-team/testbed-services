package com.fooddelivery.common.dto;

import java.math.BigDecimal;

public record OrderEvent(
        Long orderId,
        String customerId,
        Long restaurantId,
        BigDecimal totalAmount,
        String status
) {}
