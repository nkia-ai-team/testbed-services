package com.commerce.payment.repository;

import com.commerce.payment.entity.SettlementSummary;
import org.springframework.data.jpa.repository.JpaRepository;

public interface SettlementSummaryRepository extends JpaRepository<SettlementSummary, Long> {
}
