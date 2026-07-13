package com.fooddelivery.order.service;

import com.fooddelivery.order.repository.OrderRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;

/**
 * order.cleanup.retention-days 를 넘긴 PENDING 주문(결제/배차가 끝내 끝나지 않은 묵은 주문)을
 * CANCELLED 로 정리한다.
 */
@Component
public class OrderCleanupBatch {

    private static final Logger log = LoggerFactory.getLogger(OrderCleanupBatch.class);

    private final OrderRepository orderRepository;
    private final int retentionDays;

    public OrderCleanupBatch(OrderRepository orderRepository,
                              @Value("${order.cleanup.retention-days:30}") int retentionDays) {
        this.orderRepository = orderRepository;
        this.retentionDays = retentionDays;
    }

    @Scheduled(fixedDelayString = "${order.cleanup.interval-ms:3600000}",
            initialDelayString = "${order.cleanup.interval-ms:3600000}")
    @Transactional
    public void cleanupStaleOrders() {
        LocalDateTime threshold = LocalDateTime.now().minusDays(retentionDays);
        log.info("Stale order cleanup batch started: retentionDays={}, threshold={}", retentionDays, threshold);
        int cancelled = orderRepository.cancelStalePending(threshold);
        log.info("Stale order cleanup batch finished: cancelled={}", cancelled);
    }
}
