package com.fooddelivery.restaurant.entity;

import jakarta.persistence.*;
import java.time.LocalDateTime;

@Entity
@Table(name = "menu_popularity_summary")
public class MenuPopularitySummary {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "menu_id", nullable = false)
    private Long menuId;

    @Column(name = "restaurant_id", nullable = false)
    private Long restaurantId;

    @Column(name = "menu_name", nullable = false, length = 128)
    private String menuName;

    @Column(name = "order_count", nullable = false)
    private long orderCount;

    @Column(name = "computed_at", updatable = false)
    private LocalDateTime computedAt = LocalDateTime.now();

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getMenuId() { return menuId; }
    public void setMenuId(Long menuId) { this.menuId = menuId; }
    public Long getRestaurantId() { return restaurantId; }
    public void setRestaurantId(Long restaurantId) { this.restaurantId = restaurantId; }
    public String getMenuName() { return menuName; }
    public void setMenuName(String menuName) { this.menuName = menuName; }
    public long getOrderCount() { return orderCount; }
    public void setOrderCount(long orderCount) { this.orderCount = orderCount; }
    public LocalDateTime getComputedAt() { return computedAt; }
}
