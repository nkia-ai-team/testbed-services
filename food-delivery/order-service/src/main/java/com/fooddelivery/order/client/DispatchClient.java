package com.fooddelivery.order.client;

import com.fooddelivery.common.exception.ServiceException;
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
}
