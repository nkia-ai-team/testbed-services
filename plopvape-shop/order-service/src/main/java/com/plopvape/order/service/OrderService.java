package com.plopvape.order.service;

import com.plopvape.common.dto.*;
import com.plopvape.common.exception.ServiceException;
import com.plopvape.order.client.InventoryClient;
import com.plopvape.order.client.PaymentClient;
import com.plopvape.order.client.ProductClient;
import com.plopvape.order.entity.Order;
import com.plopvape.order.entity.OrderItem;
import com.plopvape.order.event.OrderEventPublisher;
import com.plopvape.order.repository.OrderRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;

@Service
public class OrderService {

    private static final Logger log = LoggerFactory.getLogger(OrderService.class);

    private final OrderRepository orderRepository;
    private final ProductClient productClient;
    private final PaymentClient paymentClient;
    private final InventoryClient inventoryClient;
    private final OrderEventPublisher eventPublisher;

    public OrderService(OrderRepository orderRepository,
                        ProductClient productClient,
                        PaymentClient paymentClient,
                        InventoryClient inventoryClient,
                        OrderEventPublisher eventPublisher) {
        this.orderRepository = orderRepository;
        this.productClient = productClient;
        this.paymentClient = paymentClient;
        this.inventoryClient = inventoryClient;
        this.eventPublisher = eventPublisher;
    }

    @Transactional
    public OrderResponse createOrder(OrderRequest request) {
        List<ReserveStockResponse> reserved = new ArrayList<>();

        try {
            // 1. 각 상품에 대해 재고 확인 + 차감 (3-hop: order → product → inventory)
            BigDecimal totalAmount = BigDecimal.ZERO;
            for (var item : request.items()) {
                ReserveStockResponse stockResponse = productClient.reserveStock(
                        item.productId(), item.quantity());
                reserved.add(stockResponse);
                totalAmount = totalAmount.add(
                        stockResponse.price().multiply(BigDecimal.valueOf(item.quantity())));
            }

            // 2. 결제 요청
            PaymentResponse paymentResponse;
            try {
                paymentResponse = paymentClient.requestPayment(0L, totalAmount, "CARD");
            } catch (Exception ex) {
                log.error("Payment failed, releasing reserved stock: {}", ex.getMessage());
                releaseAll(request.items());
                throw new ServiceException(HttpStatus.BAD_GATEWAY, "Payment failed: " + ex.getMessage());
            }

            // 3. 주문 저장
            Order order = new Order();
            order.setCustomerName(request.customerName());
            order.setCustomerEmail(request.customerEmail());
            order.setTotalAmount(totalAmount);
            order.setStatus("PAID");

            for (int i = 0; i < request.items().size(); i++) {
                var itemReq = request.items().get(i);
                var stockRes = reserved.get(i);

                OrderItem orderItem = new OrderItem();
                orderItem.setProductId(itemReq.productId());
                orderItem.setProductName(stockRes.name());
                orderItem.setQuantity(itemReq.quantity());
                orderItem.setUnitPrice(stockRes.price());
                orderItem.setSubtotal(stockRes.price().multiply(BigDecimal.valueOf(itemReq.quantity())));
                order.addItem(orderItem);
            }

            orderRepository.save(order);

            // 4. 비동기 알림 이벤트 발행
            try {
                eventPublisher.publish(new OrderEvent(
                        order.getId(),
                        order.getCustomerName(),
                        order.getCustomerEmail(),
                        order.getTotalAmount(),
                        order.getStatus()
                ));
            } catch (Exception ex) {
                log.warn("Failed to publish order event (non-critical): {}", ex.getMessage());
            }

            return toResponse(order);

        } catch (ServiceException ex) {
            throw ex;
        } catch (Exception ex) {
            log.error("Order creation failed, releasing reserved stock: {}", ex.getMessage());
            releaseAll(request.items());
            throw new ServiceException(HttpStatus.INTERNAL_SERVER_ERROR,
                    "Order creation failed: " + ex.getMessage());
        }
    }

    @Transactional(readOnly = true)
    public List<OrderResponse> getAllOrders() {
        return orderRepository.findAll().stream().map(this::toResponse).toList();
    }

    @Transactional(readOnly = true)
    public OrderResponse getOrder(Long id) {
        Order order = orderRepository.findById(id)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Order not found: " + id));
        return toResponse(order);
    }

    private void releaseAll(List<OrderRequest.OrderItemRequest> items) {
        for (var item : items) {
            inventoryClient.releaseStock(item.productId(), item.quantity());
        }
    }

    private OrderResponse toResponse(Order order) {
        var items = order.getItems().stream()
                .map(i -> new OrderResponse.OrderItemResponse(
                        i.getProductId(),
                        i.getProductName(),
                        i.getQuantity(),
                        i.getUnitPrice(),
                        i.getSubtotal()
                )).toList();

        return new OrderResponse(
                order.getId(),
                order.getCustomerName(),
                order.getStatus(),
                order.getTotalAmount(),
                items,
                order.getCreatedAt()
        );
    }
}
