package com.plopvape.notification.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

@Service
public class NotificationService {

    private static final Logger log = LoggerFactory.getLogger(NotificationService.class);

    public void sendNotification(String orderId, String customerName,
                                  String customerEmail, String totalAmount, String status) {
        log.info("Sending notification to {}: Order {} completed - amount: {}, status: {}",
                customerEmail, orderId, totalAmount, status);

        // 알림 발송 시뮬레이션 (실제 이메일/SMS 발송 없음)
        log.info("Notification sent successfully to {} for order {}", customerName, orderId);
    }
}
