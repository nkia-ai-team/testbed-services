package com.civilpetition.web;

import org.springframework.http.HttpStatus;

/** 민원 처리 상태 흐름: RECEIVED -> IN_PROGRESS -> COMPLETED. ANSWER 는 어느 열린 상태에서든 바로 완료. */
public final class Workflow {

    private Workflow() {
    }

    public static String next(String status, String action) {
        if ("COMPLETED".equals(status)) {
            throw new ApiException(HttpStatus.CONFLICT, "already completed");
        }
        if ("ANSWER".equals(action)) {
            return "COMPLETED";
        }
        if ("ACCEPT".equals(action)) {
            if ("RECEIVED".equals(status)) {
                return "IN_PROGRESS";
            }
            throw new ApiException(HttpStatus.CONFLICT, "already accepted");
        }
        throw new ApiException(HttpStatus.BAD_REQUEST, "unknown action");
    }
}
