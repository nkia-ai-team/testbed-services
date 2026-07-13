package com.commerce.common.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record PromotionResponse(
        Long promotionId,
        String name,
        String description,
        BigDecimal discountPercent,
        LocalDateTime startsAt,
        LocalDateTime endsAt,
        boolean active
) {}
