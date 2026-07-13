package com.fooddelivery.order.client;

import com.fooddelivery.common.dto.DispatchRequest;
import com.fooddelivery.common.dto.DispatchResponse;
import com.fooddelivery.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.util.Map;

@Component
public class DispatchClient {

    private static final Logger log = LoggerFactory.getLogger(DispatchClient.class);
    private final RestClient dispatchRestClient;

    public DispatchClient(RestClient dispatchRestClient) {
        this.dispatchRestClient = dispatchRestClient;
    }

    /**
     * 가용 courier capacity 조회.
     * 응답: { "currentAssigned": int, "maxCapacity": int, "available": int }
     */
    @SuppressWarnings("unchecked")
    @CircuitBreaker(name = "dispatch", fallbackMethod = "checkCapacityFallback")
    @Retry(name = "dispatch")
    public Map<String, Integer> checkCapacity() {
        try {
            return dispatchRestClient.get()
                    .uri("/api/deliveries/capacity")
                    .retrieve()
                    .body(Map.class);
        } catch (RestClientException ex) {
            log.error("Failed to check dispatch capacity: {}", ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Dispatch capacity check failed: " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private Map<String, Integer> checkCapacityFallback(Throwable ex) {
        throw new ServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Dispatch service unavailable: " + ex.getMessage());
    }

    /**
     * 실제 courier 배차 — order 생성 후 fan-out 호출.
     */
    @CircuitBreaker(name = "dispatch", fallbackMethod = "dispatchCourierFallback")
    @Retry(name = "dispatch")
    public DispatchResponse dispatchCourier(Long orderId, String region) {
        try {
            return dispatchRestClient.post()
                    .uri("/api/deliveries/dispatch")
                    .body(new DispatchRequest(orderId, region))
                    .retrieve()
                    .body(DispatchResponse.class);
        } catch (RestClientException ex) {
            log.error("Failed to dispatch courier for order {}: {}", orderId, ex.getMessage());
            throw new ServiceException(HttpStatus.SERVICE_UNAVAILABLE,
                    "Dispatch service failed: " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private DispatchResponse dispatchCourierFallback(Long orderId, String region, Throwable ex) {
        throw new ServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Dispatch service unavailable: " + ex.getMessage());
    }
}
