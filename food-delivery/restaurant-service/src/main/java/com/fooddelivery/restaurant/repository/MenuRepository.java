package com.fooddelivery.restaurant.repository;

import com.fooddelivery.restaurant.entity.Menu;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.LocalDateTime;
import java.util.List;

public interface MenuRepository extends JpaRepository<Menu, Long> {
    List<Menu> findByRestaurantId(Long restaurantId);

    // (8번 배치) 인기 메뉴 집계 — food-delivery 는 서비스별 스키마 분리가 없는 단일 flat MySQL DB라
    // order_items/orders 를 네이티브 쿼리로 직접 조인해 집계할 수 있다(다른 서비스 소유 테이블이지만
    // 동일 물리 DB 내). lookback 기간 내 주문에 포함된 메뉴별 주문 건수를 집계.
    @Query(value = "SELECT oi.menu_id AS menuId, m.restaurant_id AS restaurantId, m.name AS menuName, COUNT(*) AS orderCount "
            + "FROM order_items oi "
            + "JOIN orders o ON o.id = oi.order_id "
            + "JOIN menus m ON m.id = oi.menu_id "
            + "WHERE o.created_at >= :since "
            + "GROUP BY oi.menu_id, m.restaurant_id, m.name "
            + "ORDER BY orderCount DESC",
            nativeQuery = true)
    List<PopularMenuProjection> aggregatePopularMenus(@Param("since") LocalDateTime since);

    interface PopularMenuProjection {
        Long getMenuId();
        Long getRestaurantId();
        String getMenuName();
        Long getOrderCount();
    }
}
