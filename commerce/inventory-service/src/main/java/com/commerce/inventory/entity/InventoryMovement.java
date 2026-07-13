package com.commerce.inventory.entity;

import jakarta.persistence.*;
import java.time.LocalDateTime;

/**
 * 재고 변동 이력(원장). reserve/release/updateStock 호출마다 한 행씩 남긴다.
 * quantity는 부호 있는 변화량(RESERVE는 음수, RELEASE/증가성 ADJUST는 양수).
 * resultingStock은 그 변동을 반영한 직후의 inventory.stock 값 — 재조정 배치가
 * "우리 원장이 마지막으로 기록한 값"과 "지금 실제 stock"을 비교하는 기준이 된다.
 */
@Entity
@Table(name = "inventory_movements", schema = "inventory_schema")
public class InventoryMovement {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "product_id", nullable = false)
    private Long productId;

    @Column(name = "movement_type", nullable = false, length = 20)
    private String movementType;

    @Column(nullable = false)
    private int quantity;

    @Column(name = "resulting_stock", nullable = false)
    private int resultingStock;

    @Column(name = "created_at", updatable = false)
    private LocalDateTime createdAt = LocalDateTime.now();

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getProductId() { return productId; }
    public void setProductId(Long productId) { this.productId = productId; }
    public String getMovementType() { return movementType; }
    public void setMovementType(String movementType) { this.movementType = movementType; }
    public int getQuantity() { return quantity; }
    public void setQuantity(int quantity) { this.quantity = quantity; }
    public int getResultingStock() { return resultingStock; }
    public void setResultingStock(int resultingStock) { this.resultingStock = resultingStock; }
    public LocalDateTime getCreatedAt() { return createdAt; }
}
