package com.commerce.common.dto;

public record InventoryReserveResponse(
        Long productId,
        boolean reserved,
        int remainingStock
) {}
