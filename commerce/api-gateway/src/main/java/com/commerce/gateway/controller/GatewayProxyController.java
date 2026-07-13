package com.commerce.gateway.controller;

import com.commerce.gateway.service.AuthGuard;
import com.commerce.gateway.service.ProxyService;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 엣지 라우팅: client → api-gateway → 각 도메인 서비스.
 * 라우트별로 메서드를 분리해 ProxyService의 서비스별 circuit breaker/retry 인스턴스와 1:1 대응시킨다.
 */
@RestController
public class GatewayProxyController {

    private final AuthGuard authGuard;
    private final ProxyService proxyService;

    public GatewayProxyController(AuthGuard authGuard, ProxyService proxyService) {
        this.authGuard = authGuard;
        this.proxyService = proxyService;
    }

    @RequestMapping("/api/users/**")
    public ResponseEntity<byte[]> users(HttpServletRequest request, @RequestBody(required = false) byte[] body) {
        authGuard.verifyIfWrite(request);
        return proxyService.forwardToUser(request, body);
    }

    @RequestMapping("/api/carts/**")
    public ResponseEntity<byte[]> carts(HttpServletRequest request, @RequestBody(required = false) byte[] body) {
        authGuard.verifyIfWrite(request);
        return proxyService.forwardToCart(request, body);
    }

    @RequestMapping("/api/pricing/**")
    public ResponseEntity<byte[]> pricing(HttpServletRequest request, @RequestBody(required = false) byte[] body) {
        authGuard.verifyIfWrite(request);
        return proxyService.forwardToPricing(request, body);
    }

    @RequestMapping("/api/shipments/**")
    public ResponseEntity<byte[]> shipments(HttpServletRequest request, @RequestBody(required = false) byte[] body) {
        authGuard.verifyIfWrite(request);
        return proxyService.forwardToShipping(request, body);
    }

    @RequestMapping("/api/orders/**")
    public ResponseEntity<byte[]> orders(HttpServletRequest request, @RequestBody(required = false) byte[] body) {
        authGuard.verifyIfWrite(request);
        return proxyService.forwardToOrder(request, body);
    }

    @RequestMapping("/api/products/**")
    public ResponseEntity<byte[]> products(HttpServletRequest request, @RequestBody(required = false) byte[] body) {
        authGuard.verifyIfWrite(request);
        return proxyService.forwardToProduct(request, body);
    }
}
