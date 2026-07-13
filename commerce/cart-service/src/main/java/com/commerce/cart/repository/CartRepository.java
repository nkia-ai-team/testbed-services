package com.commerce.cart.repository;

import com.commerce.cart.entity.Cart;
import org.springframework.data.jpa.repository.JpaRepository;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

public interface CartRepository extends JpaRepository<Cart, Long> {

    Optional<Cart> findByUserId(Long userId);

    List<Cart> findByUpdatedAtBefore(LocalDateTime cutoff);
}
