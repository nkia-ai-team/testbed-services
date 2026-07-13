package com.commerce.shipping;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.domain.EntityScan;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.scheduling.annotation.EnableScheduling;

// commerce-common의 outbox 패키지는 기본 스캔 범위(com.commerce.shipping) 밖이라 명시적으로 포함시킨다.
@SpringBootApplication
@EnableScheduling
@EntityScan(basePackages = {"com.commerce.shipping.entity", "com.commerce.common.outbox"})
@EnableJpaRepositories(basePackages = {"com.commerce.shipping.repository", "com.commerce.common.outbox"})
@ComponentScan(basePackages = {"com.commerce.shipping", "com.commerce.common.outbox"})
public class ShippingServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(ShippingServiceApplication.class, args);
    }
}
