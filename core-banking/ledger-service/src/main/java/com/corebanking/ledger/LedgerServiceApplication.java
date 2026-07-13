package com.corebanking.ledger;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.domain.EntityScan;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@EnableScheduling
@EntityScan(basePackages = {"com.corebanking.ledger.entity", "com.corebanking.common.outbox"})
@EnableJpaRepositories(basePackages = {"com.corebanking.ledger.repository", "com.corebanking.common.outbox"})
@ComponentScan(basePackages = {"com.corebanking.ledger", "com.corebanking.common.outbox"})
public class LedgerServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(LedgerServiceApplication.class, args);
    }
}
