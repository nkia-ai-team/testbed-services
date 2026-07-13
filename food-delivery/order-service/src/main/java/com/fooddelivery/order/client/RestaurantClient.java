package com.fooddelivery.order.client;

import com.fooddelivery.common.dto.MenuResponse;
import com.fooddelivery.common.dto.RestaurantResponse;
import com.fooddelivery.common.exception.ServiceException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.util.List;

@Component
public class RestaurantClient {

    private static final Logger log = LoggerFactory.getLogger(RestaurantClient.class);
    private final RestClient restaurantRestClient;

    public RestaurantClient(RestClient restaurantRestClient) {
        this.restaurantRestClient = restaurantRestClient;
    }

    @CircuitBreaker(name = "restaurant", fallbackMethod = "getRestaurantFallback")
    @Retry(name = "restaurant")
    public RestaurantResponse getRestaurant(Long restaurantId) {
        try {
            return restaurantRestClient.get()
                    .uri("/api/restaurants/{id}", restaurantId)
                    .retrieve()
                    .body(RestaurantResponse.class);
        } catch (RestClientException ex) {
            log.error("Failed to fetch restaurant {}: {}", restaurantId, ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Restaurant lookup failed: " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private RestaurantResponse getRestaurantFallback(Long restaurantId, Throwable ex) {
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "Restaurant service unavailable: " + ex.getMessage());
    }

    @CircuitBreaker(name = "restaurant", fallbackMethod = "getMenuFallback")
    @Retry(name = "restaurant")
    public List<MenuResponse> getMenu(Long restaurantId) {
        try {
            return restaurantRestClient.get()
                    .uri("/api/restaurants/{id}/menu", restaurantId)
                    .retrieve()
                    .body(new org.springframework.core.ParameterizedTypeReference<List<MenuResponse>>() {});
        } catch (RestClientException ex) {
            log.error("Failed to fetch menu for restaurant {}: {}", restaurantId, ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Menu lookup failed: " + ex.getMessage());
        }
    }

    @SuppressWarnings("unused")
    private List<MenuResponse> getMenuFallback(Long restaurantId, Throwable ex) {
        throw new ServiceException(HttpStatus.BAD_GATEWAY, "Restaurant service unavailable: " + ex.getMessage());
    }
}
