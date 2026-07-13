package com.commerce.shipping.event;

import com.commerce.common.dto.OrderEvent;
import com.commerce.shipping.service.ShippingService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

/**
 * commerce.orders 구독 — 결제완료(PAID) 주문에 대해 배송을 생성한다.
 * order-service가 outbox 릴레이로 발행하는 이벤트(ORDER_PAID 등)를 그대로 소비.
 */
@Component
public class OrderEventConsumer {

    private static final Logger log = LoggerFactory.getLogger(OrderEventConsumer.class);

    private final ShippingService shippingService;
    private final ObjectMapper objectMapper;

    public OrderEventConsumer(ShippingService shippingService, ObjectMapper objectMapper) {
        this.shippingService = shippingService;
        this.objectMapper = objectMapper;
    }

    @KafkaListener(topics = "${topics.orders}", groupId = "shipping-service")
    public void onOrderEvent(String message) {
        try {
            OrderEvent event = objectMapper.readValue(message, OrderEvent.class);
            if ("PAID".equals(event.status())) {
                shippingService.createShipmentForOrder(event.orderId());
            }
        } catch (Exception ex) {
            log.error("Failed to process order event: {}", ex.getMessage(), ex);
        }
    }
}
