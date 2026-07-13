package com.fooddelivery.payment.repository;

import com.fooddelivery.payment.entity.Payment;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.LocalDateTime;
import java.util.List;

public interface PaymentRepository extends JpaRepository<Payment, Long> {

    List<Payment> findByStatusInAndSettledAtIsNullAndCreatedAtLessThan(List<String> statuses, LocalDateTime cutoff);

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
