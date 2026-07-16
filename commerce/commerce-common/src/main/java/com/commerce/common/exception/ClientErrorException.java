package com.commerce.common.exception;

import org.springframework.http.HttpStatus;

// 하류의 정상 업무 거절(4xx)을 나타낸다 — 가용성 장애가 아니므로
// resilience4j CircuitBreaker/Retry의 ignore-exceptions 대상이다.
public class ClientErrorException extends ServiceException {
    public ClientErrorException(HttpStatus status, String message) {
        super(status, message);
    }
}
