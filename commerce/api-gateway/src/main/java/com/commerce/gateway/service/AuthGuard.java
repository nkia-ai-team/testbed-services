package com.commerce.gateway.service;

import com.commerce.common.dto.TokenVerifyResponse;
import com.commerce.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import java.util.Set;

/**
 * 쓰기성 요청(POST/PUT/DELETE/PATCH)의 Authorization 토큰을 user-service verify-token으로 검증한다.
 * 회원가입/로그인 자체는 토큰이 없는 상태로 호출되므로 예외 대상.
 */
@Component
public class AuthGuard {

    private static final Set<String> WRITE_METHODS = Set.of("POST", "PUT", "DELETE", "PATCH");
    private static final Set<String> EXEMPT_PATHS = Set.of("/api/users/register", "/api/users/login");

    private final RestClient userRestClient;

    public AuthGuard(@Qualifier("userRestClient") RestClient userRestClient) {
        this.userRestClient = userRestClient;
    }

    public void verifyIfWrite(HttpServletRequest request) {
        if (!WRITE_METHODS.contains(request.getMethod())) {
            return;
        }
        if (EXEMPT_PATHS.contains(request.getRequestURI())) {
            return;
        }

        String token = extractToken(request);
        if (token == null || token.isBlank()) {
            throw new ServiceException(HttpStatus.UNAUTHORIZED, "Missing Authorization token");
        }

        TokenVerifyResponse result = verifyToken(token);
        if (result == null || !result.valid()) {
            throw new ServiceException(HttpStatus.UNAUTHORIZED, "Invalid or expired token");
        }
    }

    // 브레이커 open/장애 시 인증 경로는 fail-close(401)한다 — 검증 불가 토큰을 통과시키지 않는다.
    @CircuitBreaker(name = "user", fallbackMethod = "verifyTokenFallback")
    @Retry(name = "user")
    public TokenVerifyResponse verifyToken(String token) {
        return userRestClient.post()
                .uri(uriBuilder -> uriBuilder.path("/api/users/verify-token").queryParam("token", token).build())
                .retrieve()
                .body(TokenVerifyResponse.class);
    }

    @SuppressWarnings("unused")
    private TokenVerifyResponse verifyTokenFallback(String token, Throwable ex) {
        throw new ServiceException(HttpStatus.UNAUTHORIZED, "Token verification unavailable: " + ex.getMessage());
    }

    private String extractToken(HttpServletRequest request) {
        String header = request.getHeader("Authorization");
        if (header == null) {
            return null;
        }
        return header.startsWith("Bearer ") ? header.substring(7) : header;
    }
}
