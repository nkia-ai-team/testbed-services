package com.fooddelivery.restaurant.service;

import com.fooddelivery.common.dto.MenuResponse;
import com.fooddelivery.common.dto.PopularMenuResponse;
import com.fooddelivery.common.dto.RestaurantResponse;
import com.fooddelivery.common.exception.ServiceException;
import com.fooddelivery.restaurant.entity.Menu;
import com.fooddelivery.restaurant.entity.MenuPopularitySummary;
import com.fooddelivery.restaurant.entity.Restaurant;
import com.fooddelivery.restaurant.repository.MenuPopularitySummaryRepository;
import com.fooddelivery.restaurant.repository.MenuRepository;
import com.fooddelivery.restaurant.repository.RestaurantRepository;
import org.springframework.data.domain.Pageable;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

@Service
public class RestaurantService {

    private final RestaurantRepository restaurantRepository;
    private final MenuRepository menuRepository;
    private final MenuPopularitySummaryRepository popularitySummaryRepository;

    public RestaurantService(RestaurantRepository restaurantRepository,
                              MenuRepository menuRepository,
                              MenuPopularitySummaryRepository popularitySummaryRepository) {
        this.restaurantRepository = restaurantRepository;
        this.menuRepository = menuRepository;
        this.popularitySummaryRepository = popularitySummaryRepository;
    }

    // §7 신규 — region/status 필터 + 페이지네이션. 배열 응답(하위호환 원칙, page/size 미지정 시 무제한).
    @Transactional(readOnly = true)
    public List<RestaurantResponse> searchRestaurants(String region, String status, Pageable pageable) {
        return restaurantRepository.search(region, status, pageable).getContent().stream()
                .map(r -> new RestaurantResponse(r.getId(), r.getName(), r.getRegion(), r.getStatus()))
                .toList();
    }

    // §7 신규 — PopularMenuBatch가 채운 캐시 테이블 조회.
    @Transactional(readOnly = true)
    public List<PopularMenuResponse> getPopularMenu(Long restaurantId) {
        if (!restaurantRepository.existsById(restaurantId)) {
            throw new ServiceException(HttpStatus.NOT_FOUND, "Restaurant not found: " + restaurantId);
        }
        return popularitySummaryRepository.findByRestaurantIdOrderByOrderCountDesc(restaurantId).stream()
                .map(s -> new PopularMenuResponse(s.getMenuId(), s.getMenuName(), s.getOrderCount()))
                .toList();
    }

    @Transactional(readOnly = true)
    public RestaurantResponse getRestaurant(Long id) {
        Restaurant r = restaurantRepository.findById(id)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Restaurant not found: " + id));
        return new RestaurantResponse(r.getId(), r.getName(), r.getRegion(), r.getStatus());
    }

    @Transactional(readOnly = true)
    public List<MenuResponse> getMenu(Long restaurantId) {
        if (!restaurantRepository.existsById(restaurantId)) {
            throw new ServiceException(HttpStatus.NOT_FOUND, "Restaurant not found: " + restaurantId);
        }
        return menuRepository.findByRestaurantId(restaurantId).stream()
                .map(this::toMenuResponse)
                .toList();
    }

    private MenuResponse toMenuResponse(Menu m) {
        return new MenuResponse(
                m.getId(),
                m.getRestaurantId(),
                m.getCategoryId(),
                m.getName(),
                m.getPrice(),
                Boolean.TRUE.equals(m.getAvailable())
        );
    }
}
