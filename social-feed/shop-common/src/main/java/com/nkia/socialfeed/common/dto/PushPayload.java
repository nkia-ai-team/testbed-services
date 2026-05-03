package com.nkia.socialfeed.common.dto;

public record PushPayload(
        Long userId,
        String type,
        String title,
        String body
) {
}
