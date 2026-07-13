package com.fooddelivery.restaurant.repository;

import com.fooddelivery.restaurant.entity.Restaurant;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface RestaurantRepository extends JpaRepository<Restaurant, Long> {

    // §7 신규 — region/status 선택적 필터 조합.
    @Query("SELECT r FROM Restaurant r WHERE "
            + "(:region IS NULL OR r.region = :region) AND "
            + "(:status IS NULL OR r.status = :status)")
    Page<Restaurant> search(@Param("region") String region, @Param("status") String status, Pageable pageable);
}
