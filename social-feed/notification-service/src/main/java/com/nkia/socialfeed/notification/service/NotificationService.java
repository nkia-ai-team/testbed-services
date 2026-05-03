package com.nkia.socialfeed.notification.service;

import com.nkia.socialfeed.common.dto.NotificationRequest;
import com.nkia.socialfeed.common.dto.NotificationResponse;
import com.nkia.socialfeed.common.dto.PushPayload;
import com.nkia.socialfeed.notification.client.PushGatewayClient;
import com.nkia.socialfeed.notification.entity.Notification;
import com.nkia.socialfeed.notification.repository.NotificationRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

@Service
public class NotificationService {

    private static final Logger log = LoggerFactory.getLogger(NotificationService.class);

    private final NotificationRepository notificationRepository;
    private final PushGatewayClient pushGatewayClient;

    public NotificationService(NotificationRepository notificationRepository,
                               PushGatewayClient pushGatewayClient) {
        this.notificationRepository = notificationRepository;
        this.pushGatewayClient = pushGatewayClient;
    }

    /**
     * 알림 발행 + 외부 push gateway 호출.
     *
     * failure_surface: external-timeout — push gateway latency / down 시 BAD_GATEWAY.
     */
    @Transactional
    public NotificationResponse publish(NotificationRequest request) {
        Notification n = new Notification();
        n.setUserId(request.userId());
        n.setType(request.type());
        n.setRefId(request.refId());
        n.setReadFlag(0);
        notificationRepository.save(n);

        // 외부 push gateway 호출 (네트워크 의존)
        try {
            pushGatewayClient.send(new PushPayload(
                    request.userId(),
                    request.type(),
                    "New " + request.type(),
                    "ref=" + request.refId()
            ));
        } catch (Exception ex) {
            // 알림 DB 저장은 유지하되, push 실패는 상위 트랜잭션에 전파.
            // 시나리오 실험 시 이 부분이 외부 의존 실패 surface 로 노출됨.
            log.warn("Push delivery failed (notification stored). reason={}", ex.getMessage());
            throw ex;
        }

        return toResponse(n);
    }

    @Transactional(readOnly = true)
    public List<NotificationResponse> getNotificationsForUser(Long userId) {
        return notificationRepository.findByUserIdOrderByIdDesc(userId).stream()
                .map(this::toResponse)
                .toList();
    }

    private NotificationResponse toResponse(Notification n) {
        return new NotificationResponse(
                n.getId(),
                n.getUserId(),
                n.getType(),
                n.getRefId(),
                n.isRead(),
                n.getCreatedAt()
        );
    }
}
