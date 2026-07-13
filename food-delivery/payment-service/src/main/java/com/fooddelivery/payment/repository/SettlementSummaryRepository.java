package com.fooddelivery.payment.repository;

import com.fooddelivery.payment.entity.SettlementSummary;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;

public interface SettlementSummaryRepository extends JpaRepository<SettlementSummary, Long> {

    Page<SettlementSummary> findAllByOrderByPeriodEndDesc(Pageable pageable);
}
