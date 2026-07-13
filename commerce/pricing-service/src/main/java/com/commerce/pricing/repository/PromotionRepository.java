package com.commerce.pricing.repository;

import com.commerce.pricing.entity.Promotion;
import org.springframework.data.jpa.repository.JpaRepository;

import java.time.LocalDateTime;
import java.util.List;

public interface PromotionRepository extends JpaRepository<Promotion, Long> {

    List<Promotion> findByActiveTrueAndStartsAtBeforeAndEndsAtAfter(LocalDateTime now1, LocalDateTime now2);
}
