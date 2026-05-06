package com.fooddelivery.order.service;

import com.fooddelivery.common.dto.OrderRequest;
import com.fooddelivery.common.dto.OrderResponse;
import com.fooddelivery.common.exception.ServiceException;
import com.fooddelivery.order.entity.Order;
import com.fooddelivery.order.entity.OrderItem;
import com.fooddelivery.order.repository.OrderItemRepository;
import com.fooddelivery.order.repository.OrderRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;

@Service
public class OrderService {

    private static final Logger log = LoggerFactory.getLogger(OrderService.class);

    private final OrderRepository orderRepository;
    private final OrderItemRepository orderItemRepository;

    public OrderService(OrderRepository orderRepository, OrderItemRepository orderItemRepository) {
        this.orderRepository = orderRepository;
        this.orderItemRepository = orderItemRepository;
    }

    @Transactional
    public OrderResponse createOrder(OrderRequest request) {
        Order order = new Order();
        order.setCustomerId(request.customerId());
        order.setRestaurantId(request.restaurantId());
        order.setStatus("PENDING");

        BigDecimal total = BigDecimal.ZERO;
        if (request.items() != null) {
            for (var item : request.items()) {
                BigDecimal unitPrice = item.unitPrice() != null ? item.unitPrice() : BigDecimal.ZERO;
                total = total.add(unitPrice.multiply(BigDecimal.valueOf(item.qty())));
            }
        }
        order.setTotalAmount(total);
        orderRepository.save(order);

        if (request.items() != null) {
            for (var item : request.items()) {
                OrderItem oi = new OrderItem();
                oi.setOrderId(order.getId());
                oi.setMenuId(item.menuId());
                oi.setQty(item.qty());
                oi.setUnitPrice(item.unitPrice() != null ? item.unitPrice() : BigDecimal.ZERO);
                orderItemRepository.save(oi);
            }
        }

        log.info("Created order id={}, customer={}, total={}", order.getId(), order.getCustomerId(), total);
        return toResponse(order);
    }

    @Transactional(readOnly = true)
    public OrderResponse getOrder(Long id) {
        Order order = orderRepository.findById(id)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Order not found: " + id));
        return toResponse(order);
    }

    private OrderResponse toResponse(Order order) {
        return new OrderResponse(
                order.getId(),
                order.getCustomerId(),
                order.getRestaurantId(),
                order.getTotalAmount(),
                order.getStatus(),
                order.getCreatedAt()
        );
    }
}
