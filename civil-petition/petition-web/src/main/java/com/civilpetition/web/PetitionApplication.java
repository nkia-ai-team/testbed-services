package com.civilpetition.web;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.support.SpringBootServletInitializer;

/** 민원 접수, 처리 포털. 독립 Tomcat 에 WAR 로 배포한다(설계 spec-testbed-vm-3tier.md §2-3). */
@SpringBootApplication
public class PetitionApplication extends SpringBootServletInitializer {

    @Override
    protected SpringApplicationBuilder configure(SpringApplicationBuilder builder) {
        return builder.sources(PetitionApplication.class);
    }

    public static void main(String[] args) {
        SpringApplication.run(PetitionApplication.class, args);
    }
}
