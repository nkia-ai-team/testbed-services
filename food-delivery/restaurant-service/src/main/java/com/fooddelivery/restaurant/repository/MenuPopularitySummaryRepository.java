package com.fooddelivery.restaurant.repository;

import com.fooddelivery.restaurant.entity.MenuPopularitySummary;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface MenuPopularitySummaryRepository extends JpaRepository<MenuPopularitySummary, Long> {

    Page<MenuPopularitySummary> findAllByOrderByOrderCountDesc(Pageable pageable);

    List<MenuPopularitySummary> findByRestaurantIdOrderByOrderCountDesc(Long restaurantId);
}
