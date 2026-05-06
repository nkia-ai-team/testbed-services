package com.fooddelivery.common.dto;

import java.util.List;

public record OrderRequest(
        String customerId,
        Long restaurantId,
        List<OrderItemRequest> items
) {
    public record OrderItemRequest(
            Long menuId,
            int qty,
            java.math.BigDecimal unitPrice
    ) {}
}
