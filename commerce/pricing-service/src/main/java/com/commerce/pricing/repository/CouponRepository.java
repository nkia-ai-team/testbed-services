package com.commerce.pricing.repository;

import com.commerce.pricing.entity.Coupon;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;

public interface CouponRepository extends JpaRepository<Coupon, Long> {

    Optional<Coupon> findByCodeAndActiveTrue(String code);
}
