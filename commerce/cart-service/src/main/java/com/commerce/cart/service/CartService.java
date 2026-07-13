package com.commerce.cart.service;

import com.commerce.cart.entity.Cart;
import com.commerce.cart.entity.CartItem;
import com.commerce.cart.repository.CartItemRepository;
import com.commerce.cart.repository.CartRepository;
import com.commerce.common.dto.CartItemRequest;
import com.commerce.common.dto.CartItemResponse;
import com.commerce.common.dto.CartResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.retry.annotation.Retry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;

@Service
public class CartService {

    private static final Logger log = LoggerFactory.getLogger(CartService.class);

    private final CartRepository cartRepository;
    private final CartItemRepository cartItemRepository;
    private final RedisTemplate<String, String> cartRedisTemplate;
    private final ObjectMapper objectMapper;
    private final long cacheTtlSeconds;
    private final long idleExpiryHours;

    public CartService(CartRepository cartRepository,
                        CartItemRepository cartItemRepository,
                        RedisTemplate<String, String> cartRedisTemplate,
                        ObjectMapper objectMapper,
                        @Value("${cart.cache.ttl-seconds:1800}") long cacheTtlSeconds,
                        @Value("${cart.cleanup.idle-expiry-hours:24}") long idleExpiryHours) {
        this.cartRepository = cartRepository;
        this.cartItemRepository = cartItemRepository;
        this.cartRedisTemplate = cartRedisTemplate;
        this.objectMapper = objectMapper;
        this.cacheTtlSeconds = cacheTtlSeconds;
        this.idleExpiryHours = idleExpiryHours;
    }

    // 유휴 cart 정리 배치 — 5분마다(§5) 오래(idleExpiryHours) 갱신되지 않은 cart를 DB에서 삭제하고
    // Redis 캐시도 함께 무효화한다. 상주 배경 부하가 계속 carts를 만드는 전제라 정리 없이는 상태가
    // 무한히 누적된다.
    @Scheduled(fixedDelayString = "${cart.cleanup.interval-ms:300000}")
    @Transactional
    public void cleanupExpiredCarts() {
        LocalDateTime cutoff = LocalDateTime.now().minusHours(idleExpiryHours);
        List<Cart> stale = cartRepository.findByUpdatedAtBefore(cutoff);
        log.info("Cart cleanup batch started: staleCandidates={}", stale.size());

        for (Cart cart : stale) {
            cartItemRepository.deleteByCartId(cart.getId());
            cartRepository.delete(cart);
            cartRedisTemplate.delete(cacheKey(cart.getUserId()));
        }

        log.info("Cart cleanup batch finished: removed={}", stale.size());
    }

    // 캐시 우선 조회. Redis timeout/장애 시 CircuitBreaker가 열리며 DB fallback으로 즉시 우회한다
    // (캐시 스탬피드 시 Redis로의 재시도 폭주를 막고 DB만 일관되게 부담).
    @CircuitBreaker(name = "cartRedis", fallbackMethod = "getCartFromDb")
    @Retry(name = "cartRedis")
    public CartResponse getCart(Long userId) {
        String cached = cartRedisTemplate.opsForValue().get(cacheKey(userId));
        if (cached != null) {
            return deserialize(cached);
        }
        CartResponse fromDb = loadFromDb(userId);
        writeThroughCache(userId, fromDb);
        return fromDb;
    }

    @SuppressWarnings("unused") // resilience4j fallback signature (원 메서드 인자 + Throwable)
    private CartResponse getCartFromDb(Long userId, Throwable ex) {
        log.warn("Redis unavailable, falling back to DB for cart userId={}: {}", userId, ex.toString());
        return loadFromDb(userId);
    }

    @Transactional
    public CartResponse addItem(Long userId, CartItemRequest request) {
        Cart cart = getOrCreateCart(userId);
        CartItem item = cartItemRepository.findByCartIdAndProductId(cart.getId(), request.productId())
                .orElseGet(() -> {
                    CartItem newItem = new CartItem();
                    newItem.setCartId(cart.getId());
                    newItem.setProductId(request.productId());
                    newItem.setQuantity(0);
                    return newItem;
                });
        item.setQuantity(item.getQuantity() + request.quantity());
        cartItemRepository.save(item);

        return refreshCache(userId);
    }

    @Transactional
    public CartResponse updateItem(Long userId, Long productId, int quantity) {
        Cart cart = getOrCreateCart(userId);
        if (quantity <= 0) {
            cartItemRepository.findByCartIdAndProductId(cart.getId(), productId)
                    .ifPresent(cartItemRepository::delete);
        } else {
            CartItem item = cartItemRepository.findByCartIdAndProductId(cart.getId(), productId)
                    .orElseGet(() -> {
                        CartItem newItem = new CartItem();
                        newItem.setCartId(cart.getId());
                        newItem.setProductId(productId);
                        return newItem;
                    });
            item.setQuantity(quantity);
            cartItemRepository.save(item);
        }
        return refreshCache(userId);
    }

    @Transactional
    public CartResponse removeItem(Long userId, Long productId) {
        Cart cart = getOrCreateCart(userId);
        cartItemRepository.findByCartIdAndProductId(cart.getId(), productId)
                .ifPresent(cartItemRepository::delete);
        return refreshCache(userId);
    }

    @Transactional
    public CartResponse clearCart(Long userId) {
        Cart cart = getOrCreateCart(userId);
        cartItemRepository.deleteByCartId(cart.getId());
        return refreshCache(userId);
    }

    private Cart getOrCreateCart(Long userId) {
        return cartRepository.findByUserId(userId).orElseGet(() -> {
            Cart cart = new Cart();
            cart.setUserId(userId);
            return cartRepository.save(cart);
        });
    }

    private CartResponse refreshCache(Long userId) {
        CartResponse response = loadFromDb(userId);
        try {
            writeThroughCache(userId, response);
        } catch (Exception ex) {
            // 캐시 갱신 실패는 non-critical — 다음 조회 시 getCart의 CircuitBreaker 경로가 처리한다.
            log.warn("Failed to refresh cart cache for userId={}: {}", userId, ex.getMessage());
        }
        return response;
    }

    private CartResponse loadFromDb(Long userId) {
        Cart cart = getOrCreateCart(userId);
        List<CartItemResponse> items = cartItemRepository.findByCartId(cart.getId()).stream()
                .map(i -> new CartItemResponse(i.getProductId(), i.getQuantity()))
                .toList();
        return new CartResponse(userId, items, LocalDateTime.now());
    }

    private void writeThroughCache(Long userId, CartResponse response) {
        try {
            String json = objectMapper.writeValueAsString(response);
            cartRedisTemplate.opsForValue().set(cacheKey(userId), json, Duration.ofSeconds(cacheTtlSeconds));
        } catch (Exception ex) {
            log.warn("Failed to write cart cache for userId={}: {}", userId, ex.getMessage());
        }
    }

    private CartResponse deserialize(String json) {
        try {
            return objectMapper.readValue(json, CartResponse.class);
        } catch (Exception ex) {
            throw new IllegalStateException("Failed to deserialize cached cart: " + ex.getMessage(), ex);
        }
    }

    private String cacheKey(Long userId) {
        return "cart:" + userId;
    }
}
