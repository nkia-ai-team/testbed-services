package com.civilpetition.web;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/** 3초 넘게 걸린 요청을 앱 로그에 WARN 으로 남긴다(운영 앱의 흔한 느린 요청 기록). */
@Component
public class SlowRequestFilter extends OncePerRequestFilter {

    static final long SLOW_MS = 3000;
    private static final Logger log = LoggerFactory.getLogger(SlowRequestFilter.class);

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        long started = System.nanoTime();
        try {
            chain.doFilter(request, response);
        } finally {
            long took = (System.nanoTime() - started) / 1_000_000;
            if (took >= SLOW_MS) {
                log.warn("slow request method={} uri={} status={} took_ms={}",
                        request.getMethod(), request.getRequestURI(), response.getStatus(), took);
            }
        }
    }
}
