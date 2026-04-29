package com.plopvape.common.dto;

public record InventoryReleaseRequest(
        Long productId,
        int quantity
) {}
