package com.civilpetition.web;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** 오류 응답. DB 오류는 앱 로그에 ERROR 로 남기고 503 을 낸다. */
@RestControllerAdvice
public class ApiExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(ApiExceptionHandler.class);

    @ExceptionHandler(ApiException.class)
    public ResponseEntity<Map<String, String>> business(ApiException error) {
        return ResponseEntity.status(error.status()).body(Map.of("error", error.getMessage()));
    }

    @ExceptionHandler(DataAccessException.class)
    public ResponseEntity<Map<String, String>> database(DataAccessException error) {
        log.error("database access failed: {}", error.getMostSpecificCause().toString(), error);
        return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body(Map.of("error", "database unavailable"));
    }
}
