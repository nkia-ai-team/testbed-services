package com.corebanking.transfer;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.domain.EntityScan;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@EnableScheduling
@EntityScan(basePackages = {"com.corebanking.transfer.entity", "com.corebanking.common.outbox"})
@EnableJpaRepositories(basePackages = {"com.corebanking.transfer.repository", "com.corebanking.common.outbox"})
@ComponentScan(basePackages = {"com.corebanking.transfer", "com.corebanking.common.outbox"})
public class TransferServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(TransferServiceApplication.class, args);
    }
}
