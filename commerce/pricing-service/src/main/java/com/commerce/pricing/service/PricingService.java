package com.commerce.pricing.service;

import com.commerce.common.dto.*;
import com.commerce.common.exception.ServiceException;
import com.commerce.pricing.entity.Coupon;
import com.commerce.pricing.entity.Price;
import com.commerce.pricing.entity.Promotion;
import com.commerce.pricing.repository.CouponRepository;
import com.commerce.pricing.repository.PriceRepository;
import com.commerce.pricing.repository.PromotionRepository;
import io.github.resilience4j.bulkhead.annotation.Bulkhead;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDateTime;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicReference;

@Service
public class PricingService {

    private static final Logger log = LoggerFactory.getLogger(PricingService.class);

    private final PriceRepository priceRepository;
    private final PromotionRepository promotionRepository;
    private final CouponRepository couponRepository;

    // 프로모션 갱신 배치(§5, 매시)가 채우는 인메모리 캐시. quote 계산은 DB를 매번 조회하지 않고
    // 이 캐시를 읽는다 — 최대 1시간 지연을 감수하는 대신 견적 계산 경로의 DB 부하를 줄인다.
    private final AtomicReference<List<Promotion>> activePromotionsCache = new AtomicReference<>(List.of());

    public PricingService(PriceRepository priceRepository,
                           PromotionRepository promotionRepository,
                           CouponRepository couponRepository) {
        this.priceRepository = priceRepository;
        this.promotionRepository = promotionRepository;
        this.couponRepository = couponRepository;
    }

    @PostConstruct
    public void initPromotionCache() {
        refreshPromotionCache();
    }

    @Scheduled(cron = "0 0 * * * *")
    public void refreshPromotionCache() {
        log.info("Promotion cache refresh started");
        LocalDateTime now = LocalDateTime.now();
        List<Promotion> active = promotionRepository.findByActiveTrueAndStartsAtBeforeAndEndsAtAfter(now, now);
        activePromotionsCache.set(active);
        log.info("Promotion cache refresh finished: activeCount={}", active.size());
    }

    public PriceResponse getPrice(Long productId) {
        Price price = findPrice(productId);
        return new PriceResponse(price.getProductId(), price.getBasePrice());
    }

    public List<PromotionResponse> getPromotions() {
        return promotionRepository.findAll().stream().map(this::toPromotionResponse).toList();
    }

    public List<CouponResponse> getCoupons() {
        return couponRepository.findAll().stream().map(this::toCouponResponse).toList();
    }

    // order → pricing 견적 계산은 결제 직전 동기 경로라 트래픽이 몰리기 쉽다.
    // bulkhead로 동시 실행 수를 제한해 특정 순간의 과부하가 프로세스 전체로 번지지 않게 격리한다.
    @Bulkhead(name = "pricingQuote")
    public QuoteResponse calculateQuote(QuoteRequest request) {
        List<QuoteResponse.QuoteItemResult> items = request.items().stream()
                .map(item -> {
                    Price price = findPrice(item.productId());
                    BigDecimal subtotal = price.getBasePrice().multiply(BigDecimal.valueOf(item.quantity()));
                    return new QuoteResponse.QuoteItemResult(item.productId(), item.quantity(), price.getBasePrice(), subtotal);
                }).toList();

        BigDecimal subtotal = items.stream()
                .map(QuoteResponse.QuoteItemResult::subtotal)
                .reduce(BigDecimal.ZERO, BigDecimal::add);

        Optional<Promotion> bestPromotion = findBestActivePromotion();
        BigDecimal promotionDiscount = bestPromotion
                .map(p -> subtotal.multiply(p.getDiscountPercent()).divide(BigDecimal.valueOf(100), 2, RoundingMode.HALF_UP))
                .orElse(BigDecimal.ZERO);
        BigDecimal afterPromotion = subtotal.subtract(promotionDiscount);

        Coupon coupon = null;
        if (request.couponCode() != null && !request.couponCode().isBlank()) {
            coupon = couponRepository.findByCodeAndActiveTrue(request.couponCode())
                    .orElseThrow(() -> new ServiceException(HttpStatus.BAD_REQUEST,
                            "Invalid or inactive coupon code: " + request.couponCode()));
        }
        BigDecimal couponDiscount = BigDecimal.ZERO;
        if (coupon != null) {
            BigDecimal percentPart = afterPromotion.multiply(coupon.getDiscountPercent())
                    .divide(BigDecimal.valueOf(100), 2, RoundingMode.HALF_UP);
            couponDiscount = coupon.getDiscountAmount().add(percentPart);
        }

        BigDecimal total = afterPromotion.subtract(couponDiscount);
        if (total.compareTo(BigDecimal.ZERO) < 0) {
            total = BigDecimal.ZERO;
        }

        return new QuoteResponse(
                items,
                subtotal,
                promotionDiscount,
                couponDiscount,
                total,
                bestPromotion.map(Promotion::getName).orElse(null),
                coupon != null ? coupon.getCode() : null
        );
    }

    private Optional<Promotion> findBestActivePromotion() {
        return activePromotionsCache.get().stream()
                .max(Comparator.comparing(Promotion::getDiscountPercent));
    }

    private Price findPrice(Long productId) {
        return priceRepository.findByProductId(productId)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Price not found for product: " + productId));
    }

    private PromotionResponse toPromotionResponse(Promotion p) {
        return new PromotionResponse(p.getId(), p.getName(), p.getDescription(), p.getDiscountPercent(),
                p.getStartsAt(), p.getEndsAt(), p.isActive());
    }

    private CouponResponse toCouponResponse(Coupon c) {
        return new CouponResponse(c.getId(), c.getCode(), c.getDiscountAmount(), c.getDiscountPercent(), c.isActive());
    }
}
