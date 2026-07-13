package com.fooddelivery.restaurant.service;

import com.fooddelivery.restaurant.entity.MenuPopularitySummary;
import com.fooddelivery.restaurant.repository.MenuPopularitySummaryRepository;
import com.fooddelivery.restaurant.repository.MenuRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 최근 lookback-days 기간의 주문을 집계해 메뉴별 인기 순위를 캐시 테이블(menu_popularity_summary)에
 * 다시 채운다. 매 주기 전체 재계산이라 이전 회차 결과는 비우고 새로 채운다.
 */
@Component
public class PopularMenuBatch {

    private static final Logger log = LoggerFactory.getLogger(PopularMenuBatch.class);

    private final MenuRepository menuRepository;
    private final MenuPopularitySummaryRepository summaryRepository;
    private final int lookbackDays;

    public PopularMenuBatch(MenuRepository menuRepository,
                             MenuPopularitySummaryRepository summaryRepository,
                             @Value("${menu.popularity.lookback-days:7}") int lookbackDays) {
        this.menuRepository = menuRepository;
        this.summaryRepository = summaryRepository;
        this.lookbackDays = lookbackDays;
    }

    @Scheduled(fixedDelayString = "${menu.popularity.interval-ms:3600000}",
            initialDelayString = "${menu.popularity.interval-ms:3600000}")
    @Transactional
    public void run() {
        LocalDateTime since = LocalDateTime.now().minusDays(lookbackDays);
        log.info("Popular menu aggregation batch started: lookbackDays={}", lookbackDays);

        List<MenuRepository.PopularMenuProjection> rows = menuRepository.aggregatePopularMenus(since);
        summaryRepository.deleteAll();
        rows.forEach(row -> {
            MenuPopularitySummary summary = new MenuPopularitySummary();
            summary.setMenuId(row.getMenuId());
            summary.setRestaurantId(row.getRestaurantId());
            summary.setMenuName(row.getMenuName());
            summary.setOrderCount(row.getOrderCount());
            summaryRepository.save(summary);
        });

        log.info("Popular menu aggregation batch finished: menusRanked={}", rows.size());
    }
}
