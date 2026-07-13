package com.commerce.common.dto;

import java.util.List;

public record UserOverviewResponse(
        UserResponse user,
        CartResponse cart,
        List<OrderResponse> recentOrders,
        ShipmentResponse latestShipment
) {}
