package com.plopvape.common.dto;

public record InventoryReserveResponse(
        Long productId,
        boolean reserved,
        int remainingStock
) {}
