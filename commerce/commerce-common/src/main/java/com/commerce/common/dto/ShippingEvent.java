package com.commerce.common.dto;

public record ShippingEvent(
        Long shipmentId,
        Long orderId,
        String status,
        String eventType
) {}
