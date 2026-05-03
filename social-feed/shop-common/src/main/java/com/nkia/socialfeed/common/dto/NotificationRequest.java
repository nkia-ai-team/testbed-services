package com.nkia.socialfeed.common.dto;

public record NotificationRequest(
        Long userId,
        String type,
        Long refId
) {
}
