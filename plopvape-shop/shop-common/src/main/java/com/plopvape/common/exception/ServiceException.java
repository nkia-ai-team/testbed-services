package com.plopvape.common.exception;

import org.springframework.http.HttpStatus;

public class ServiceException extends RuntimeException {

    private final HttpStatus status;

    public ServiceException(HttpStatus status, String message) {
        super(message);
        this.status = status;
    }

    public ServiceException(String message) {
        this(HttpStatus.INTERNAL_SERVER_ERROR, message);
    }

    public HttpStatus getStatus() {
        return status;
    }
}
