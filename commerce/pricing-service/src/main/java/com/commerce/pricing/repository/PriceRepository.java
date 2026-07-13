package com.commerce.pricing.repository;

import com.commerce.pricing.entity.Price;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;

public interface PriceRepository extends JpaRepository<Price, Long> {

    Optional<Price> findByProductId(Long productId);
}
