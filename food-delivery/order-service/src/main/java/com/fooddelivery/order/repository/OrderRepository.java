package com.fooddelivery.order.repository;

import com.fooddelivery.order.entity.Order;
import org.springframework.data.domain.Slice;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.LocalDateTime;
import java.util.List;

public interface OrderRepository extends JpaRepository<Order, Long> {

    // (8번 배치) 결제/배차가 끝내 완료되지 않고 PENDING 에 남은 "묵은" 주문 정리.
    @Modifying
    @Query("UPDATE Order o SET o.status = 'CANCELLED' WHERE o.status = 'PENDING' AND o.createdAt < :threshold")
    int cancelStalePending(@Param("threshold") LocalDateTime threshold);

    // §7 신규 — customerId/status/restaurantId/from/to 선택적 필터 조합. 응답이 배열이라 Slice 다(COUNT 없음).
    @Query("SELECT o FROM Order o WHERE "
            + "(:customerId IS NULL OR o.customerId = :customerId) AND "
            + "(:status IS NULL OR o.status = :status) AND "
            + "(:restaurantId IS NULL OR o.restaurantId = :restaurantId) AND "
            + "(:from IS NULL OR o.createdAt >= :from) AND "
            + "(:to IS NULL OR o.createdAt <= :to)")
    Slice<Order> search(@Param("customerId") String customerId,
                         @Param("status") String status,
                         @Param("restaurantId") Long restaurantId,
                         @Param("from") LocalDateTime from,
                         @Param("to") LocalDateTime to,
                         Pageable pageable);

    // §7 신규 — 최근 N일 일별 주문수/합계 (챗봇 추세 질의 재료).
    @Query(value = "SELECT DATE(created_at) AS orderDate, COUNT(*) AS orderCount, COALESCE(SUM(total_amount), 0) AS totalAmount "
            + "FROM orders WHERE created_at >= :since "
            + "GROUP BY DATE(created_at) ORDER BY orderDate",
            nativeQuery = true)
    List<DailyOrderStatProjection> aggregateDailyStats(@Param("since") LocalDateTime since);

    interface DailyOrderStatProjection {
        java.sql.Date getOrderDate();
        long getOrderCount();
        java.math.BigDecimal getTotalAmount();
    }

    /** 보존 기간 정리 대상 판정에 필요한 세 컬럼만 읽는다. */
    interface RetentionHead {
        Long getId();

        String getStatus();

        LocalDateTime getCreatedAt();
    }

    // 보존 기간 정리용. PK 만 타고 커서 뒤 한 묶음을 읽는다 — 가장 오래된 주문이 id 앞쪽에 있다.
    @Query("select o.id as id, o.status as status, o.createdAt as createdAt from Order o "
            + "where o.id > :afterId order by o.id asc")
    List<RetentionHead> findRetentionHeads(@Param("afterId") long afterId, Pageable pageable);
}
