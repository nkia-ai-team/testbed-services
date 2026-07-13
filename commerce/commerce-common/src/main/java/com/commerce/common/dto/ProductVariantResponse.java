package com.commerce.common.dto;

import java.math.BigDecimal;

public record ProductVariantResponse(
        Long id,
        Long productId,
        String sku,
        String variantName,
        BigDecimal priceDelta
) {}
