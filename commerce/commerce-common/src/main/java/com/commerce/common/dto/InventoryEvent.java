package com.commerce.common.dto;

public record InventoryEvent(
        Long productId,
        String eventType,
        int quantity,
        int currentStock
) {}
