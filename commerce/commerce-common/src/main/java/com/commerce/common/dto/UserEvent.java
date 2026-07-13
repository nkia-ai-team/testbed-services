package com.commerce.common.dto;

public record UserEvent(
        Long userId,
        String email,
        String name,
        String eventType
) {}
