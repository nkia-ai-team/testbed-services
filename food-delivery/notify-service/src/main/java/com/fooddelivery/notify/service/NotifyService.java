package com.fooddelivery.notify.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

@Service
public class NotifyService {

    private static final Logger log = LoggerFactory.getLogger(NotifyService.class);

    public void sendNotification(String orderId, String customerId,
                                 String restaurantId, String totalAmount, String status) {
        log.info("Sending notification for order {}: customer={}, restaurant={}, amount={}, status={}",
                orderId, customerId, restaurantId, totalAmount, status);

        // 알림 발송 시뮬레이션 (실제 이메일/SMS/push 발송 없음)
        log.info("Notification sent successfully for order {}", orderId);
    }
}
