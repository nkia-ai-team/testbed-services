package com.plopvape.common.dto;

import java.util.List;

public record OrderRequest(
        String customerName,
        String customerEmail,
        List<OrderItemRequest> items
) {
    public record OrderItemRequest(
            Long productId,
            int quantity
    ) {}
}
