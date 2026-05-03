package com.nkia.socialfeed.notification.client;

import com.nkia.socialfeed.common.dto.PushPayload;
import com.nkia.socialfeed.common.exception.ServiceException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;

@Component
public class PushGatewayClient {

    private static final Logger log = LoggerFactory.getLogger(PushGatewayClient.class);

    private final RestClient pushGatewayRestClient;

    public PushGatewayClient(RestClient pushGatewayRestClient) {
        this.pushGatewayRestClient = pushGatewayRestClient;
    }

    /**
     * 외부 push gateway 호출.
     *
     * failure_surface: external-timeout.
     * mock-push-gateway 컨테이너가 응답 지연 또는 down 시 read-timeout 발생.
     */
    public void send(PushPayload payload) {
        try {
            pushGatewayRestClient.post()
                    .uri("/push")
                    .body(payload)
                    .retrieve()
                    .toBodilessEntity();
            log.debug("Push sent to gateway for user={}", payload.userId());
        } catch (ResourceAccessException ex) {
            log.error("Push gateway timeout/unreachable: {}", ex.getMessage());
            throw new ServiceException(HttpStatus.GATEWAY_TIMEOUT,
                    "Push gateway unreachable: " + ex.getMessage());
        } catch (Exception ex) {
            log.error("Push gateway error: {}", ex.getMessage());
            throw new ServiceException(HttpStatus.BAD_GATEWAY,
                    "Push gateway error: " + ex.getMessage());
        }
    }
}
