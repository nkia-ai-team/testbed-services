package com.corebanking.api;

import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration;
import org.springframework.boot.autoconfigure.orm.jpa.HibernateJpaAutoConfiguration;
import org.springframework.boot.SpringApplication;

/**
 * 이체 요청 진입점(gateway). DB 를 직접 쓰지 않으므로 JPA/DataSource 자동설정을 제외한다
 * (shop-common 이 spring-boot-starter-data-jpa 를 classpath 로 끌어오기 때문).
 */
@SpringBootApplication(exclude = {DataSourceAutoConfiguration.class, HibernateJpaAutoConfiguration.class})
public class ApiServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(ApiServiceApplication.class, args);
    }
}
