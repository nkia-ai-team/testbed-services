package com.commerce.order.event;

import com.commerce.common.dto.PaymentEvent;
import com.commerce.order.service.OrderService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

/**
 * commerce.payments 구독 — 결제 확정 이벤트로 주문 상태를 비동기 보정한다(additive).
 * 기존 동기 REST 흐름(OrderService.createOrder)은 그대로 유지하고, 이 리스너는
 * 사후 정정 용도로만 동작하며 멱등하게 처리한다(OrderService.reconcileStatusFromPayment).
 */
@Component
public class PaymentEventConsumer {

    private static final Logger log = LoggerFactory.getLogger(PaymentEventConsumer.class);

    private final OrderService orderService;
    private final ObjectMapper objectMapper;

    public PaymentEventConsumer(OrderService orderService, ObjectMapper objectMapper) {
        this.orderService = orderService;
        this.objectMapper = objectMapper;
    }

    @KafkaListener(topics = "${topics.payments}", groupId = "order-service")
    public void onPaymentEvent(String message) {
        try {
            PaymentEvent event = objectMapper.readValue(message, PaymentEvent.class);
            orderService.reconcileStatusFromPayment(event.orderId(), event.status());
        } catch (Exception ex) {
            log.error("Failed to process payment event: {}", ex.getMessage(), ex);
        }
    }
}
