package com.commerce.order.repository;

import com.commerce.order.entity.Order;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.LocalDateTime;
import java.util.List;

public interface OrderRepository extends JpaRepository<Order, Long> {

    List<Order> findByUserIdOrderByCreatedAtDesc(Long userId);

    // §7 확장: userId/status/from/to를 전부 선택적으로 조합 — null인 조건은 무시된다.
    // 기존 userId= 단일 필터 호출도 이 쿼리로 흡수되어 동일하게 동작한다(하위호환).
    @Query("SELECT o FROM Order o WHERE "
            + "(:userId IS NULL OR o.userId = :userId) AND "
            + "(:status IS NULL OR o.status = :status) AND "
            + "(:from IS NULL OR o.createdAt >= :from) AND "
            + "(:to IS NULL OR o.createdAt <= :to)")
    Page<Order> search(@Param("userId") Long userId,
                        @Param("status") String status,
                        @Param("from") LocalDateTime from,
                        @Param("to") LocalDateTime to,
                        Pageable pageable);

    // 챗봇 추세/집계 질의 재료 — 일별 주문수·금액. FUNCTION('DATE', ...)은 PostgreSQL의
    // date(timestamp) 캐스트로 그대로 번역된다.
    @Query("SELECT FUNCTION('DATE', o.createdAt), COUNT(o), COALESCE(SUM(o.totalAmount), 0) "
            + "FROM Order o WHERE o.createdAt >= :since "
            + "GROUP BY FUNCTION('DATE', o.createdAt) ORDER BY FUNCTION('DATE', o.createdAt)")
    List<Object[]> dailyStats(@Param("since") LocalDateTime since);
}
