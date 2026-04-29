package com.plopvape.payment.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.time.Duration;

@Configuration
public class RestClientConfig {

    @Value("${pg.api.base-url}")
    private String pgApiBaseUrl;

    @Value("${pg.api.connect-timeout:3s}")
    private Duration connectTimeout;

    @Value("${pg.api.read-timeout:10s}")
    private Duration readTimeout;

    @Bean
    public RestClient pgRestClient() {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(connectTimeout);
        factory.setReadTimeout(readTimeout);

        return RestClient.builder()
                .baseUrl(pgApiBaseUrl)
                .requestFactory(factory)
                .build();
    }
}
