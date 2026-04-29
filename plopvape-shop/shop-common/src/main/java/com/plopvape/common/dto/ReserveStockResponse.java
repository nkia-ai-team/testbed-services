package com.plopvape.common.dto;

import java.math.BigDecimal;

public record ReserveStockResponse(
        Long productId,
        String name,
        BigDecimal price,
        boolean reserved,
        int remainingStock
) {}
