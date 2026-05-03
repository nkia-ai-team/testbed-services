package com.nkia.socialfeed.common.dto;

import java.time.LocalDateTime;

public record NotificationResponse(
        Long id,
        Long userId,
        String type,
        Long refId,
        boolean read,
        LocalDateTime createdAt
) {
}
