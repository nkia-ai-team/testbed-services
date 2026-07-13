package com.fooddelivery.notify.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

@Service
public class NotifyService {

    private static final Logger log = LoggerFactory.getLogger(NotifyService.class);

    public void sendNotification(String eventKind, String rawPayload) {
        log.info("Sending {} notification: {}", eventKind, rawPayload);
        // 알림 발송 시뮬레이션 (실제 이메일/SMS/push 발송 없음)
        log.info("Notification sent successfully for {} event", eventKind);
    }
}
