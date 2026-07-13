package com.commerce.common.dto;

import java.math.BigDecimal;

public record PriceResponse(
        Long productId,
        BigDecimal basePrice
) {}
