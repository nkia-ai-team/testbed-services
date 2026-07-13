package com.commerce.common.dto;

import java.util.List;

public record QuoteRequest(
        List<QuoteItemRequest> items,
        String couponCode
) {
    public record QuoteItemRequest(
            Long productId,
            int quantity
    ) {}
}
