package com.commerce.cart.entity;

import com.commerce.common.config.BaseEntity;
import jakarta.persistence.*;

@Entity
@Table(name = "carts", schema = "cart_schema")
public class Cart extends BaseEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "user_id", nullable = false, unique = true)
    private Long userId;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getUserId() { return userId; }
    public void setUserId(Long userId) { this.userId = userId; }
}
