package com.plopvape.payment.repository;

import com.plopvape.payment.entity.PaymentLog;
import org.springframework.data.jpa.repository.JpaRepository;

public interface PaymentLogRepository extends JpaRepository<PaymentLog, Long> {
}
