package com.fooddelivery.payment.repository;

import com.fooddelivery.payment.entity.Payment;
import org.springframework.data.domain.Slice;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.LocalDateTime;
import java.util.List;

public interface PaymentRepository extends JpaRepository<Payment, Long> {

    List<Payment> findByStatusInAndSettledAtIsNullAndCreatedAtLessThan(List<String> statuses, LocalDateTime cutoff);

    // §7 신규 — status/from/to 선택적 필터 조합. 응답이 배열이라 Slice 다(COUNT 없음).
    @Query("SELECT p FROM Payment p WHERE "
            + "(:status IS NULL OR p.status = :status) AND "
            + "(:from IS NULL OR p.createdAt >= :from) AND "
            + "(:to IS NULL OR p.createdAt <= :to)")
    Slice<Payment> search(@Param("status") String status,
                           @Param("from") LocalDateTime from,
                           @Param("to") LocalDateTime to,
                           Pageable pageable);

    /** 보존 기간 정리 대상 판정에 필요한 컬럼만 읽는다. */
    interface RetentionHead {
        Long getId();

        String getStatus();

        LocalDateTime getCreatedAt();

        LocalDateTime getProcessedAt();

        LocalDateTime getSettledAt();
    }

    // 보존 기간 정리용. PK 만 타고 커서 뒤 한 묶음을 읽는다 — 가장 오래된 결제가 id 앞쪽에 있다.
    @Query("select p.id as id, p.status as status, p.createdAt as createdAt, p.processedAt as processedAt, "
            + "p.settledAt as settledAt from Payment p where p.id > :afterId order by p.id asc")
    List<RetentionHead> findRetentionHeads(@Param("afterId") long afterId, Pageable pageable);
}
