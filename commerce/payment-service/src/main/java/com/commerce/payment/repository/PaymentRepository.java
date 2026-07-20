package com.commerce.payment.repository;

import com.commerce.payment.entity.Payment;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

public interface PaymentRepository extends JpaRepository<Payment, Long> {

    // settlement 배치 집계 결과 — 건수·합계·기간 시작만 DB에서 계산해 받는다.
    interface SettleableAggregate {
        long getPaymentCount();
        BigDecimal getTotalAmount();
        LocalDateTime getPeriodStart();
    }

    // settlement 배치 대상 집계: 아직 정산(은행 이체) 반영이 안 된 완료 결제.
    // 이체 실패로 settledAt이 안 채워진 건은 이후 주기에도 계속 이 쿼리에 다시 잡힌다.
    // 엔티티 목록 로딩 금지 — 이월 누적(수십만 건)이 힙에 올라와 OOM으로 죽는다(2026-07-20 실증).
    @Query("SELECT COUNT(p) AS paymentCount, COALESCE(SUM(p.amount), 0) AS totalAmount, "
            + "MIN(p.createdAt) AS periodStart FROM Payment p "
            + "WHERE p.status IN :statuses AND p.settledAt IS NULL AND p.createdAt < :cutoff")
    SettleableAggregate aggregateSettleable(@Param("statuses") List<String> statuses,
                                             @Param("cutoff") LocalDateTime cutoff);

    // settlement 이체 성공 시 대상 전체를 벌크로 정산 마감한다(집계와 동일 조건).
    @Modifying(clearAutomatically = true)
    @Query("UPDATE Payment p SET p.settledAt = :settledAt "
            + "WHERE p.status IN :statuses AND p.settledAt IS NULL AND p.createdAt < :cutoff")
    int markSettled(@Param("statuses") List<String> statuses,
                    @Param("cutoff") LocalDateTime cutoff,
                    @Param("settledAt") LocalDateTime settledAt);

    // §7 신규 — status/from/to 선택적 필터 조합.
    @Query("SELECT p FROM Payment p WHERE "
            + "(:status IS NULL OR p.status = :status) AND "
            + "(:from IS NULL OR p.createdAt >= :from) AND "
            + "(:to IS NULL OR p.createdAt <= :to)")
    Page<Payment> search(@Param("status") String status,
                          @Param("from") LocalDateTime from,
                          @Param("to") LocalDateTime to,
                          Pageable pageable);
}
