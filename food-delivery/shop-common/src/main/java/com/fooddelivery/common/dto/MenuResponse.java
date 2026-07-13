package com.fooddelivery.common.dto;

import java.math.BigDecimal;

public record MenuResponse(
        Long id,
        Long restaurantId,
        Long categoryId,
        String name,
        BigDecimal price,
        boolean available
) {}
