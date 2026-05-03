package com.nkia.socialfeed.notification.controller;

import com.nkia.socialfeed.common.dto.NotificationRequest;
import com.nkia.socialfeed.common.dto.NotificationResponse;
import com.nkia.socialfeed.notification.service.NotificationService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
public class NotificationController {

    private final NotificationService notificationService;

    public NotificationController(NotificationService notificationService) {
        this.notificationService = notificationService;
    }

    @PostMapping("/api/notifications")
    public ResponseEntity<NotificationResponse> publish(@RequestBody NotificationRequest request) {
        return ResponseEntity.ok(notificationService.publish(request));
    }

    @GetMapping("/api/notifications/{userId}")
    public ResponseEntity<List<NotificationResponse>> getByUser(@PathVariable Long userId) {
        return ResponseEntity.ok(notificationService.getNotificationsForUser(userId));
    }
}
