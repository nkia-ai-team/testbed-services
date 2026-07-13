package com.commerce.common.dto;

import java.math.BigDecimal;

public record CouponResponse(
        Long couponId,
        String code,
        BigDecimal discountAmount,
        BigDecimal discountPercent,
        boolean active
) {}
