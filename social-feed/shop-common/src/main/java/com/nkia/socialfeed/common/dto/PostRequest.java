package com.nkia.socialfeed.common.dto;

public record PostRequest(
        Long authorId,
        String content
) {
}
