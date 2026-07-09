package com.plopvape.common.dto;

import java.math.BigDecimal;

public record ProductResponse(
        Long id,
        String name,
        String description,
        BigDecimal price,
        String categoryName,
        String imageUrl,
        Integer stock
) {}
