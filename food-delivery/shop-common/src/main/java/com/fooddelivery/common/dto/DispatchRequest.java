package com.fooddelivery.common.dto;

public record DispatchRequest(
        Long orderId,
        String region
) {}
