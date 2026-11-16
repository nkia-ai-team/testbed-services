package com.civilpetition.web;

import org.springframework.http.HttpStatus;

/** 업무 규칙으로 거절한 요청. 응답 코드와 짧은 사유를 함께 낸다. */
public class ApiException extends RuntimeException {

    private final HttpStatus status;

    public ApiException(HttpStatus status, String reason) {
        super(reason);
        this.status = status;
    }

    public HttpStatus status() {
        return status;
    }
}
