package com.corebanking.transfer.entity;

import jakarta.persistence.*;
import java.math.BigDecimal;

/**
 * transfer-service 관점의 accounts 매핑. 잔액 차감 시 PESSIMISTIC_WRITE(=SELECT ... FOR UPDATE)
 * 로 lock 을 잡아 이체 트랜잭션 간 row-lock 경합을 만든다(장애 표면: lock-contention).
 */
@Entity
@Table(name = "accounts")
public class Account {

    @Id
    @Column(name = "id", length = 64)
    private String id;

    @Column(name = "holder", length = 128)
    private String holder;

    @Column(name = "balance", precision = 18, scale = 2)
    private BigDecimal balance = BigDecimal.ZERO;

    @Column(name = "status", length = 16)
    private String status = "ACTIVE";

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getHolder() { return holder; }
    public void setHolder(String holder) { this.holder = holder; }
    public BigDecimal getBalance() { return balance; }
    public void setBalance(BigDecimal balance) { this.balance = balance; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
}
