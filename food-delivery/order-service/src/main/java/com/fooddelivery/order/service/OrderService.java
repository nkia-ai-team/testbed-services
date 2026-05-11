package com.fooddelivery.order.service;

import com.fooddelivery.common.dto.MenuResponse;
import com.fooddelivery.common.dto.OrderRequest;
import com.fooddelivery.common.dto.OrderResponse;
import com.fooddelivery.common.dto.RestaurantResponse;
import com.fooddelivery.common.exception.ServiceException;
import com.fooddelivery.order.client.DispatchClient;
import com.fooddelivery.order.client.RestaurantClient;
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
import java.util.HashMap;
import java.util.List;
import java.util.Map;

@Service
public class OrderService {

    private static final Logger log = LoggerFactory.getLogger(OrderService.class);

    private final OrderRepository orderRepository;
    private final OrderItemRepository orderItemRepository;
    private final RestaurantClient restaurantClient;
    private final DispatchClient dispatchClient;

    public OrderService(OrderRepository orderRepository,
                        OrderItemRepository orderItemRepository,
                        RestaurantClient restaurantClient,
                        DispatchClient dispatchClient) {
        this.orderRepository = orderRepository;
        this.orderItemRepository = orderItemRepository;
        this.restaurantClient = restaurantClient;
        this.dispatchClient = dispatchClient;
    }

    @Transactional
    public OrderResponse createOrder(OrderRequest request) {
        // 도메인 검증 1: restaurant 영업 상태 (CLOSED 거부)
        RestaurantResponse restaurant = restaurantClient.getRestaurant(request.restaurantId());
        if (restaurant == null) {
            throw new ServiceException(HttpStatus.BAD_REQUEST,
                    "Restaurant not found: " + request.restaurantId());
        }
        if (!"OPEN".equalsIgnoreCase(restaurant.status())) {
            log.info("Order rejected: restaurant {} status={}", restaurant.id(), restaurant.status());
            throw new ServiceException(HttpStatus.BAD_REQUEST,
                    "Restaurant is " + restaurant.status() + ": " + restaurant.name());
        }

        // 도메인 검증 2: 모든 menu item available=true (매진 거부)
        if (request.items() != null && !request.items().isEmpty()) {
            List<MenuResponse> menus = restaurantClient.getMenu(request.restaurantId());
            Map<Long, MenuResponse> byId = new HashMap<>();
            if (menus != null) {
                for (MenuResponse m : menus) {
                    byId.put(m.id(), m);
                }
            }
            for (var item : request.items()) {
                MenuResponse menu = byId.get(item.menuId());
                if (menu == null) {
                    throw new ServiceException(HttpStatus.BAD_REQUEST,
                            "Menu not found in restaurant " + request.restaurantId() + ": " + item.menuId());
                }
                if (!menu.available()) {
                    log.info("Order rejected: menu {} sold out", menu.id());
                    throw new ServiceException(HttpStatus.BAD_REQUEST,
                            "Menu sold out: " + menu.name());
                }
            }
        }

        // 도메인 검증 3: courier capacity (배달원 부족 거부)
        try {
            Map<String, Integer> cap = dispatchClient.checkCapacity();
            if (cap != null && cap.containsKey("available")) {
                int available = cap.get("available");
                if (available <= 0) {
                    log.info("Order rejected: courier pool exhausted (assigned={}, max={})",
                            cap.get("currentAssigned"), cap.get("maxCapacity"));
                    throw new ServiceException(HttpStatus.SERVICE_UNAVAILABLE,
                            "No courier available (capacity full)");
                }
            }
        } catch (ServiceException se) {
            throw se;
        } catch (Exception ex) {
            log.warn("Dispatch capacity check failed, rejecting order: {}", ex.getMessage());
            throw new ServiceException(HttpStatus.SERVICE_UNAVAILABLE,
                    "Dispatch service unreachable");
        }

        // 주문 생성
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
