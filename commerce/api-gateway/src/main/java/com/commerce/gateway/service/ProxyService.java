package com.commerce.gateway.service;

import com.commerce.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.util.Set;

/**
 * 명시적 프록시 계층. Spring Cloud Gateway 같은 프레임워크 없이 RestClient로 요청을
 * 그대로 하류 서비스에 전달한다. 라우트(하류 서비스)별로 메서드를 분리해 각각 독립된
 * Resilience4j circuit breaker/retry 인스턴스를 붙인다 — 한 서비스 장애가 다른 라우트로
 * 전파되지 않게 격리한다.
 */
@Component
public class ProxyService {

    private static final Logger log = LoggerFactory.getLogger(ProxyService.class);
    private static final Set<String> BODY_METHODS = Set.of("POST", "PUT", "PATCH");

    private final RestClient userRestClient;
    private final RestClient cartRestClient;
    private final RestClient pricingRestClient;
    private final RestClient shippingRestClient;
    private final RestClient orderRestClient;
    private final RestClient productRestClient;

    public ProxyService(@Qualifier("userRestClient") RestClient userRestClient,
                         @Qualifier("cartRestClient") RestClient cartRestClient,
                         @Qualifier("pricingRestClient") RestClient pricingRestClient,
                         @Qualifier("shippingRestClient") RestClient shippingRestClient,
                         @Qualifier("orderRestClient") RestClient orderRestClient,
                         @Qualifier("productRestClient") RestClient productRestClient) {
        this.userRestClient = userRestClient;
        this.cartRestClient = cartRestClient;
        this.pricingRestClient = pricingRestClient;
        this.shippingRestClient = shippingRestClient;
        this.orderRestClient = orderRestClient;
        this.productRestClient = productRestClient;
    }

    @CircuitBreaker(name = "user", fallbackMethod = "unavailable")
    @Retry(name = "user")
    public ResponseEntity<byte[]> forwardToUser(HttpServletRequest request, byte[] body) {
        return doForward(userRestClient, request, body);
    }

    @CircuitBreaker(name = "cart", fallbackMethod = "unavailable")
    @Retry(name = "cart")
    public ResponseEntity<byte[]> forwardToCart(HttpServletRequest request, byte[] body) {
        return doForward(cartRestClient, request, body);
    }

    @CircuitBreaker(name = "pricing", fallbackMethod = "unavailable")
    @Retry(name = "pricing")
    public ResponseEntity<byte[]> forwardToPricing(HttpServletRequest request, byte[] body) {
        return doForward(pricingRestClient, request, body);
    }

    @CircuitBreaker(name = "shipping", fallbackMethod = "unavailable")
    @Retry(name = "shipping")
    public ResponseEntity<byte[]> forwardToShipping(HttpServletRequest request, byte[] body) {
        return doForward(shippingRestClient, request, body);
    }

    @CircuitBreaker(name = "order", fallbackMethod = "unavailable")
    @Retry(name = "order")
    public ResponseEntity<byte[]> forwardToOrder(HttpServletRequest request, byte[] body) {
        return doForward(orderRestClient, request, body);
    }

    @CircuitBreaker(name = "product", fallbackMethod = "unavailable")
    @Retry(name = "product")
    public ResponseEntity<byte[]> forwardToProduct(HttpServletRequest request, byte[] body) {
        return doForward(productRestClient, request, body);
    }

    @SuppressWarnings("unused") // resilience4j fallback 시그니처(원 메서드 인자 + Throwable)
    private ResponseEntity<byte[]> unavailable(HttpServletRequest request, byte[] body, Throwable ex) {
        log.warn("Downstream unavailable for {} {}: {}", request.getMethod(), request.getRequestURI(), ex.toString());
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "Downstream service unavailable: " + ex.getMessage());
    }

    private ResponseEntity<byte[]> doForward(RestClient client, HttpServletRequest request, byte[] body) {
        String uri = request.getQueryString() != null
                ? request.getRequestURI() + "?" + request.getQueryString()
                : request.getRequestURI();
        HttpMethod method = HttpMethod.valueOf(request.getMethod());

        try {
            var spec = client.method(method).uri(uri);
            if (request.getContentType() != null) {
                spec.contentType(MediaType.parseMediaType(request.getContentType()));
            }
            if (body != null && body.length > 0 && BODY_METHODS.contains(request.getMethod())) {
                return spec.body(body).retrieve().toEntity(byte[].class);
            }
            return spec.retrieve().toEntity(byte[].class);
        } catch (RestClientResponseException ex) {
            return ResponseEntity.status(ex.getStatusCode()).body(ex.getResponseBodyAsByteArray());
        }
    }
}
