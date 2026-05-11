package com.fooddelivery.order.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.time.Duration;

@Configuration
public class RestClientConfig {

    @Bean
    public RestClient restaurantRestClient(
            @Value("${services.restaurant.url}") String baseUrl,
            @Value("${services.restaurant.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.restaurant.read-timeout:5s}") Duration readTimeout) {
        return buildClient(baseUrl, connectTimeout, readTimeout);
    }

    @Bean
    public RestClient dispatchRestClient(
            @Value("${services.dispatch.url}") String baseUrl,
            @Value("${services.dispatch.connect-timeout:3s}") Duration connectTimeout,
            @Value("${services.dispatch.read-timeout:5s}") Duration readTimeout) {
        return buildClient(baseUrl, connectTimeout, readTimeout);
    }

    private RestClient buildClient(String baseUrl, Duration connectTimeout, Duration readTimeout) {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(connectTimeout);
        factory.setReadTimeout(readTimeout);
        return RestClient.builder().baseUrl(baseUrl).requestFactory(factory).build();
    }
}
