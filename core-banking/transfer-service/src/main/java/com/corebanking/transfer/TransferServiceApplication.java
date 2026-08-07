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
// delay 패키지는 컴포넌트만 있고 엔티티가 없다 — 지연 제어 조회는 JdbcTemplate 이라
// Hibernate 가 없는 테이블에 대해 매 주기 ERROR 스택을 방송하지 않는다.
@ComponentScan(basePackages = {"com.corebanking.transfer", "com.corebanking.common.outbox",
        "com.corebanking.common.delay"})
public class TransferServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(TransferServiceApplication.class, args);
    }
}
