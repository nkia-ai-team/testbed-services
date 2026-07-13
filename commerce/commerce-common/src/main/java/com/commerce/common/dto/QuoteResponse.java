package com.commerce.common.dto;

import java.math.BigDecimal;
import java.util.List;

public record QuoteResponse(
        List<QuoteItemResult> items,
        BigDecimal subtotal,
        BigDecimal promotionDiscount,
        BigDecimal couponDiscount,
        BigDecimal total,
        String appliedPromotion,
        String appliedCoupon
) {
    public record QuoteItemResult(
            Long productId,
            int quantity,
            BigDecimal unitPrice,
            BigDecimal subtotal
    ) {}
}
