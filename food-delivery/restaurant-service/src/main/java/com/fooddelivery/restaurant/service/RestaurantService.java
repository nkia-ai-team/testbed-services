package com.fooddelivery.restaurant.service;

import com.fooddelivery.common.dto.MenuResponse;
import com.fooddelivery.common.dto.RestaurantResponse;
import com.fooddelivery.common.exception.ServiceException;
import com.fooddelivery.restaurant.entity.Menu;
import com.fooddelivery.restaurant.entity.Restaurant;
import com.fooddelivery.restaurant.repository.MenuRepository;
import com.fooddelivery.restaurant.repository.RestaurantRepository;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

@Service
public class RestaurantService {

    private final RestaurantRepository restaurantRepository;
    private final MenuRepository menuRepository;

    public RestaurantService(RestaurantRepository restaurantRepository, MenuRepository menuRepository) {
        this.restaurantRepository = restaurantRepository;
        this.menuRepository = menuRepository;
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
                m.getName(),
                m.getPrice(),
                Boolean.TRUE.equals(m.getAvailable())
        );
    }
}
