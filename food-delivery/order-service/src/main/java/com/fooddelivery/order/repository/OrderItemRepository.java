package com.fooddelivery.order.repository;

import com.fooddelivery.order.entity.OrderItem;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.util.List;

public interface OrderItemRepository extends JpaRepository<OrderItem, Long> {
    List<OrderItem> findByOrderId(Long orderId);

    // 보존 기간 정리용. order_items.order_id 가 orders 를 FK(ON DELETE 없음)로 가리키므로 주문보다
    // 먼저 idx_order_items_order 로 지운다.
    @Modifying
    @Query("delete from OrderItem i where i.orderId in :orderIds")
    int deleteByOrderIdIn(@Param("orderIds") Collection<Long> orderIds);
}
