package com.plopvape.inventory.service;

import com.plopvape.common.dto.InventoryReleaseRequest;
import com.plopvape.common.dto.InventoryReserveRequest;
import com.plopvape.common.dto.InventoryReserveResponse;
import com.plopvape.common.exception.ServiceException;
import com.plopvape.inventory.entity.Inventory;
import com.plopvape.inventory.repository.InventoryRepository;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class InventoryService {

    private final InventoryRepository inventoryRepository;

    public InventoryService(InventoryRepository inventoryRepository) {
        this.inventoryRepository = inventoryRepository;
    }

    @Transactional(readOnly = true)
    public Inventory getByProductId(Long productId) {
        return inventoryRepository.findByProductId(productId)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND,
                        "Inventory not found for product: " + productId));
    }

    @Transactional
    public InventoryReserveResponse reserve(InventoryReserveRequest request) {
        Inventory inventory = inventoryRepository.findByProductIdForUpdate(request.productId())
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND,
                        "Inventory not found for product: " + request.productId()));

        if (inventory.getStock() < request.quantity()) {
            throw new ServiceException(HttpStatus.CONFLICT,
                    "Insufficient stock for product: " + request.productId()
                            + " (available: " + inventory.getStock() + ", requested: " + request.quantity() + ")");
        }

        inventory.setStock(inventory.getStock() - request.quantity());
        inventoryRepository.save(inventory);

        return new InventoryReserveResponse(
                inventory.getProductId(),
                true,
                inventory.getStock()
        );
    }

    @Transactional
    public Inventory release(InventoryReleaseRequest request) {
        Inventory inventory = inventoryRepository.findByProductId(request.productId())
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND,
                        "Inventory not found for product: " + request.productId()));

        inventory.setStock(inventory.getStock() + request.quantity());
        return inventoryRepository.save(inventory);
    }

    @Transactional
    public Inventory updateStock(Long productId, int stock) {
        Inventory inventory = inventoryRepository.findByProductId(productId)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND,
                        "Inventory not found for product: " + productId));

        inventory.setStock(stock);
        return inventoryRepository.save(inventory);
    }
}
