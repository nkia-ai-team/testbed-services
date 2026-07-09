package com.plopvape.common.dto;

public record InventoryReserveRequest(
        Long productId,
        int quantity
) {}
