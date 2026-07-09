package com.corebanking.account.entity;

import jakarta.persistence.*;
import java.math.BigDecimal;

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
