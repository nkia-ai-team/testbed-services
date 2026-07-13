package com.commerce.common.dto;

public record CartItemResponse(
        Long productId,
        int quantity
) {}
