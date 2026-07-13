package com.commerce.gateway.service;

import com.commerce.common.dto.CartResponse;
import com.commerce.common.dto.OrderResponse;
import com.commerce.common.dto.ShipmentResponse;
import com.commerce.common.dto.UserOverviewResponse;
import com.commerce.common.dto.UserResponse;
import com.commerce.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.util.Arrays;
import java.util.List;

/**
 * BFF 집계 조회 — user/cart/order/shipping을 순차 호출해 하나의 응답으로 합성한다.
 * user는 필수(실패 시 전체 실패), 나머지는 개별 CircuitBreaker fallback으로 부분 응답을 허용한다.
 */
@Component
public class AggregationService {

    private static final Logger log = LoggerFactory.getLogger(AggregationService.class);
    private static final int RECENT_ORDERS_LIMIT = 5;

    private final RestClient userRestClient;
    private final RestClient cartRestClient;
    private final RestClient orderRestClient;
    private final RestClient shippingRestClient;

    public AggregationService(@Qualifier("userRestClient") RestClient userRestClient,
                               @Qualifier("cartRestClient") RestClient cartRestClient,
                               @Qualifier("orderRestClient") RestClient orderRestClient,
                               @Qualifier("shippingRestClient") RestClient shippingRestClient) {
        this.userRestClient = userRestClient;
        this.cartRestClient = cartRestClient;
        this.orderRestClient = orderRestClient;
        this.shippingRestClient = shippingRestClient;
    }

    public UserOverviewResponse getOverview(Long userId) {
        UserResponse user = fetchUser(userId);
        CartResponse cart = fetchCart(userId);
        List<OrderResponse> recentOrders = fetchRecentOrders(userId);
        ShipmentResponse latestShipment = recentOrders.isEmpty() ? null : fetchLatestShipment(recentOrders.get(0).orderId());

        return new UserOverviewResponse(user, cart, recentOrders, latestShipment);
    }

    // user는 overview의 앵커 — 조회 실패 시 전체를 실패시킨다(부분 응답 대상 아님).
    @CircuitBreaker(name = "user", fallbackMethod = "fetchUserFallback")
    @Retry(name = "user")
    public UserResponse fetchUser(Long userId) {
        return userRestClient.get().uri("/api/users/{id}", userId).retrieve().body(UserResponse.class);
    }

    @SuppressWarnings("unused")
    private UserResponse fetchUserFallback(Long userId, Throwable ex) {
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "User service unavailable: " + ex.getMessage());
    }

    @CircuitBreaker(name = "cart", fallbackMethod = "fetchCartFallback")
    @Retry(name = "cart")
    public CartResponse fetchCart(Long userId) {
        return cartRestClient.get().uri("/api/carts/{userId}", userId).retrieve().body(CartResponse.class);
    }

    @SuppressWarnings("unused")
    private CartResponse fetchCartFallback(Long userId, Throwable ex) {
        log.warn("Cart unavailable for overview, returning partial response: userId={}, {}", userId, ex.toString());
        return null;
    }

    @CircuitBreaker(name = "order", fallbackMethod = "fetchRecentOrdersFallback")
    @Retry(name = "order")
    public List<OrderResponse> fetchRecentOrders(Long userId) {
        OrderResponse[] orders = orderRestClient.get()
                .uri(uriBuilder -> uriBuilder.path("/api/orders").queryParam("userId", userId).build())
                .retrieve()
                .body(OrderResponse[].class);
        if (orders == null) {
            return List.of();
        }
        return Arrays.stream(orders).limit(RECENT_ORDERS_LIMIT).toList();
    }

    @SuppressWarnings("unused")
    private List<OrderResponse> fetchRecentOrdersFallback(Long userId, Throwable ex) {
        log.warn("Order service unavailable for overview, returning partial response: userId={}, {}", userId, ex.toString());
        return List.of();
    }

    @CircuitBreaker(name = "shipping", fallbackMethod = "fetchLatestShipmentFallback")
    @Retry(name = "shipping")
    public ShipmentResponse fetchLatestShipment(Long orderId) {
        try {
            return shippingRestClient.get().uri("/api/shipments/order/{orderId}", orderId).retrieve().body(ShipmentResponse.class);
        } catch (RestClientResponseException ex) {
            if (ex.getStatusCode().value() == 404) {
                return null; // 아직 배송이 생성되지 않은 정상 상태 — CB 실패로 세지 않는다.
            }
            throw ex;
        }
    }

    @SuppressWarnings("unused")
    private ShipmentResponse fetchLatestShipmentFallback(Long orderId, Throwable ex) {
        log.warn("Shipping service unavailable for overview, returning partial response: orderId={}, {}", orderId, ex.toString());
        return null;
    }
}
