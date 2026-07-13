package com.fooddelivery.common.dto;

public record PopularMenuResponse(
        Long menuId,
        String menuName,
        long orderCount
) {}
