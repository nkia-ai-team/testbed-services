package com.commerce.order.service;

import com.commerce.common.dto.*;
import com.commerce.common.exception.ServiceException;
import com.commerce.order.client.CartClient;
import com.commerce.order.client.InventoryClient;
import com.commerce.order.client.PaymentClient;
import com.commerce.order.client.PricingClient;
import com.commerce.order.client.ProductClient;
import com.commerce.order.entity.Order;
import com.commerce.order.entity.OrderItem;
import com.commerce.order.event.OrderEventPublisher;
import com.commerce.order.repository.OrderRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.Sort;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.sql.Date;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;

@Service
public class OrderService {

    private static final Logger log = LoggerFactory.getLogger(OrderService.class);

    private final OrderRepository orderRepository;
    private final ProductClient productClient;
    private final PaymentClient paymentClient;
    private final InventoryClient inventoryClient;
    private final CartClient cartClient;
    private final PricingClient pricingClient;
    private final OrderEventPublisher eventPublisher;

    public OrderService(OrderRepository orderRepository,
                        ProductClient productClient,
                        PaymentClient paymentClient,
                        InventoryClient inventoryClient,
                        CartClient cartClient,
                        PricingClient pricingClient,
                        OrderEventPublisher eventPublisher) {
        this.orderRepository = orderRepository;
        this.productClient = productClient;
        this.paymentClient = paymentClient;
        this.inventoryClient = inventoryClient;
        this.cartClient = cartClient;
        this.pricingClient = pricingClient;
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

    // checkout 5-hop: cart 조회 → pricing 최종가 확정 → inventory 예약(기존 product경유 흐름 재사용)
    // → payment 결제(금액은 quote 결과) → 주문 확정 + cart 비우기(best-effort) + outbox 이벤트.
    // 실패 시 기존 createOrder와 동일한 보상 로직(재고 해제)을 적용한다.
    @Transactional
    public OrderResponse checkout(CheckoutRequest request) {
        Long userId = request.userId();
        CartResponse cart = cartClient.getCart(userId);
        if (cart == null || cart.items() == null || cart.items().isEmpty()) {
            throw new ServiceException(HttpStatus.BAD_REQUEST, "Cart is empty for user: " + userId);
        }

        List<QuoteRequest.QuoteItemRequest> quoteItems = cart.items().stream()
                .map(i -> new QuoteRequest.QuoteItemRequest(i.productId(), i.quantity()))
                .toList();
        QuoteResponse quote = pricingClient.calculateQuote(new QuoteRequest(quoteItems, request.couponCode()));

        List<ReserveStockResponse> reserved = new ArrayList<>();
        try {
            for (var item : cart.items()) {
                reserved.add(productClient.reserveStock(item.productId(), item.quantity()));
            }

            PaymentResponse paymentResponse;
            try {
                paymentResponse = paymentClient.requestPayment(0L, quote.total(), "CARD");
            } catch (Exception ex) {
                log.error("Checkout payment failed, releasing reserved stock: userId={}, {}", userId, ex.getMessage());
                releaseAllCartItems(cart.items());
                throw new ServiceException(HttpStatus.BAD_GATEWAY, "Payment failed: " + ex.getMessage());
            }

            Order order = new Order();
            order.setUserId(userId);
            // user-service 조회는 checkout 흐름 범위 밖(스펙상 cart→pricing→inventory→payment) —
            // 고객 표시명은 최소 placeholder로 채운다. 필요 시 gateway BFF가 user 정보를 별도로 합성한다.
            order.setCustomerName("user-" + userId);
            order.setTotalAmount(quote.total());
            order.setStatus("PAID");

            for (int i = 0; i < cart.items().size(); i++) {
                var cartItem = cart.items().get(i);
                var stockRes = reserved.get(i);
                var quoteItem = quote.items().get(i);

                OrderItem orderItem = new OrderItem();
                orderItem.setProductId(cartItem.productId());
                orderItem.setProductName(stockRes.name());
                orderItem.setQuantity(cartItem.quantity());
                orderItem.setUnitPrice(quoteItem.unitPrice());
                orderItem.setSubtotal(quoteItem.subtotal());
                order.addItem(orderItem);
            }

            orderRepository.save(order);

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

            cartClient.clearCart(userId); // best-effort — 실패해도 주문은 이미 성공(클라이언트 내부에서 예외를 흡수)

            return toResponse(order);

        } catch (ServiceException ex) {
            throw ex;
        } catch (Exception ex) {
            log.error("Checkout failed, releasing reserved stock: userId={}, {}", userId, ex.getMessage());
            releaseAllCartItems(cart.items());
            throw new ServiceException(HttpStatus.INTERNAL_SERVER_ERROR, "Checkout failed: " + ex.getMessage());
        }
    }

    @Transactional(readOnly = true)
    public List<OrderResponse> getAllOrders(Long userId) {
        List<Order> orders = userId != null
                ? orderRepository.findByUserIdOrderByCreatedAtDesc(userId)
                : orderRepository.findAll();
        return orders.stream().map(this::toResponse).toList();
    }

    // §7 확장: userId/status/from/to/page/size. 응답은 기존과 동일하게 배열이다 — BFF의
    // AggregationService가 이 엔드포인트를 OrderResponse[]로 역직렬화하므로 Page 객체로
    // 감싸면 안 된다(하위호환 최우선). page/size는 조회 슬라이싱에만 사용한다.
    @Transactional(readOnly = true)
    public List<OrderResponse> searchOrders(Long userId, String status, LocalDateTime from, LocalDateTime to,
                                             Pageable pageable) {
        Sort defaultSort = Sort.by(Sort.Direction.DESC, "createdAt");
        Pageable effective;
        if (pageable.getSort().isSorted()) {
            effective = pageable;
        } else if (pageable.isPaged()) {
            effective = PageRequest.of(pageable.getPageNumber(), pageable.getPageSize(), defaultSort);
        } else {
            effective = Pageable.unpaged(defaultSort);
        }
        Page<Order> page = orderRepository.search(userId, status, from, to, effective);
        return page.map(this::toResponse).getContent();
    }

    @Transactional(readOnly = true)
    public List<DailyOrderStatResponse> getDailyStats(int days) {
        LocalDateTime since = LocalDateTime.now().minusDays(Math.max(days, 1)).toLocalDate().atStartOfDay();
        return orderRepository.dailyStats(since).stream()
                .map(row -> new DailyOrderStatResponse(
                        ((Date) row[0]).toLocalDate(),
                        ((Number) row[1]).longValue(),
                        (BigDecimal) row[2]
                ))
                .toList();
    }

    @Transactional(readOnly = true)
    public OrderResponse getOrder(Long id) {
        Order order = orderRepository.findById(id)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Order not found: " + id));
        return toResponse(order);
    }

    // Kafka commerce.payments 구독을 통한 비동기 상태 보정.
    // 동기 흐름(createOrder)에서 이미 status를 확정하므로, 여기서는 사후 정정만 additive하게 반영한다.
    // 같은 상태로의 재적용은 멱등하게 무시한다.
    @Transactional
    public void reconcileStatusFromPayment(Long orderId, String paymentStatus) {
        String resolvedStatus = switch (paymentStatus) {
            case "SUCCESS", "APPROVED", "COMPLETED" -> "PAID";
            case "FAILED", "DECLINED" -> "PAYMENT_FAILED";
            default -> null;
        };
        if (resolvedStatus == null) {
            log.warn("Unrecognized payment status for reconciliation: orderId={}, status={}", orderId, paymentStatus);
            return;
        }

        orderRepository.findById(orderId).ifPresentOrElse(order -> {
            if (!resolvedStatus.equals(order.getStatus())) {
                order.setStatus(resolvedStatus);
                orderRepository.save(order);
                log.info("Reconciled order status from payment event: orderId={}, status={}", orderId, resolvedStatus);
            }
        }, () -> log.warn("Order not found for payment reconciliation: orderId={}", orderId));
    }

    private void releaseAll(List<OrderRequest.OrderItemRequest> items) {
        for (var item : items) {
            inventoryClient.releaseStock(item.productId(), item.quantity());
        }
    }

    private void releaseAllCartItems(List<CartItemResponse> items) {
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
