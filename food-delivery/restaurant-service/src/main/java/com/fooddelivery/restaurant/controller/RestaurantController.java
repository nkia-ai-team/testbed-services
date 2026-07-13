package com.fooddelivery.restaurant.controller;

import com.fooddelivery.common.dto.MenuResponse;
import com.fooddelivery.common.dto.PopularMenuResponse;
import com.fooddelivery.common.dto.RestaurantResponse;
import com.fooddelivery.restaurant.service.RestaurantService;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.PageRequest;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/restaurants")
public class RestaurantController {

    private final RestaurantService restaurantService;

    public RestaurantController(RestaurantService restaurantService) {
        this.restaurantService = restaurantService;
    }

    // §7 신규 — region/status/page/size. page/size 둘 다 없으면 무제한(기존 목록 조회 동작 보존).
    @GetMapping
    public ResponseEntity<List<RestaurantResponse>> getRestaurants(
            @RequestParam(required = false) String region,
            @RequestParam(required = false) String status,
            @RequestParam(required = false) Integer page,
            @RequestParam(required = false) Integer size) {
        Pageable pageable = (page != null || size != null)
                ? PageRequest.of(page != null ? page : 0, size != null ? size : 20)
                : Pageable.unpaged();
        return ResponseEntity.ok(restaurantService.searchRestaurants(region, status, pageable));
    }

    @GetMapping("/{id}")
    public ResponseEntity<RestaurantResponse> getRestaurant(@PathVariable Long id) {
        return ResponseEntity.ok(restaurantService.getRestaurant(id));
    }

    @GetMapping("/{id}/menu")
    public ResponseEntity<List<MenuResponse>> getMenu(@PathVariable Long id) {
        return ResponseEntity.ok(restaurantService.getMenu(id));
    }

    // §7 신규 — PopularMenuBatch 캐시 조회.
    @GetMapping("/{id}/popular-menu")
    public ResponseEntity<List<PopularMenuResponse>> getPopularMenu(@PathVariable Long id) {
        return ResponseEntity.ok(restaurantService.getPopularMenu(id));
    }
}
