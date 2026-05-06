package com.fooddelivery.common.dto;

public record RestaurantResponse(
        Long id,
        String name,
        String region,
        String status
) {}
