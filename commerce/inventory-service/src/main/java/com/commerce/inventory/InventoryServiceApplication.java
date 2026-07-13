package com.commerce.inventory;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.domain.EntityScan;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@EnableScheduling
@EntityScan(basePackages = {"com.commerce.inventory.entity", "com.commerce.common.outbox"})
@EnableJpaRepositories(basePackages = {"com.commerce.inventory.repository", "com.commerce.common.outbox"})
@ComponentScan(basePackages = {"com.commerce.inventory", "com.commerce.common.outbox"})
public class InventoryServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(InventoryServiceApplication.class, args);
    }
}
