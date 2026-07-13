package com.commerce.order;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.domain.EntityScan;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.scheduling.annotation.EnableScheduling;

// commerce-common의 outbox 패키지(OutboxEvent/Repository/Publisher/Relay)는
// 기본 컴포넌트/엔티티 스캔 범위(com.commerce.order) 밖이라 명시적으로 포함시킨다.
@SpringBootApplication
@EnableScheduling
@EntityScan(basePackages = {"com.commerce.order.entity", "com.commerce.common.outbox"})
@EnableJpaRepositories(basePackages = {"com.commerce.order.repository", "com.commerce.common.outbox"})
@ComponentScan(basePackages = {"com.commerce.order", "com.commerce.common.outbox"})
public class OrderServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(OrderServiceApplication.class, args);
    }
}
