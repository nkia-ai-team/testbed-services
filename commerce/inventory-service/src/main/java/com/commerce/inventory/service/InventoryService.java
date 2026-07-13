package com.commerce.inventory.service;

import com.commerce.common.dto.InventoryEvent;
import com.commerce.common.dto.InventoryReleaseRequest;
import com.commerce.common.dto.InventoryReserveRequest;
import com.commerce.common.dto.InventoryReserveResponse;
import com.commerce.common.exception.ServiceException;
import com.commerce.common.outbox.OutboxPublisher;
import com.commerce.inventory.entity.Inventory;
import com.commerce.inventory.entity.InventoryMovement;
import com.commerce.inventory.repository.InventoryMovementRepository;
import com.commerce.inventory.repository.InventoryRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class InventoryService {

    private final InventoryRepository inventoryRepository;
    private final InventoryMovementRepository inventoryMovementRepository;
    private final OutboxPublisher outboxPublisher;
    private final String inventoryTopic;

    public InventoryService(InventoryRepository inventoryRepository,
                             InventoryMovementRepository inventoryMovementRepository,
                             OutboxPublisher outboxPublisher,
                             @Value("${topics.inventory}") String inventoryTopic) {
        this.inventoryRepository = inventoryRepository;
        this.inventoryMovementRepository = inventoryMovementRepository;
        this.outboxPublisher = outboxPublisher;
        this.inventoryTopic = inventoryTopic;
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
        recordMovement(inventory, "RESERVE", -request.quantity());

        publishEvent(inventory, "STOCK_RESERVED", request.quantity());

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
        inventoryRepository.save(inventory);
        recordMovement(inventory, "RELEASE", request.quantity());

        publishEvent(inventory, "STOCK_RELEASED", request.quantity());

        return inventory;
    }

    @Transactional
    public Inventory updateStock(Long productId, int stock) {
        Inventory inventory = inventoryRepository.findByProductId(productId)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND,
                        "Inventory not found for product: " + productId));

        int delta = stock - inventory.getStock();
        inventory.setStock(stock);
        inventoryRepository.save(inventory);
        recordMovement(inventory, "ADJUST", delta);

        publishEvent(inventory, "STOCK_ADJUSTED", delta);

        return inventory;
    }

    private void recordMovement(Inventory inventory, String movementType, int signedQuantity) {
        InventoryMovement movement = new InventoryMovement();
        movement.setProductId(inventory.getProductId());
        movement.setMovementType(movementType);
        movement.setQuantity(signedQuantity);
        movement.setResultingStock(inventory.getStock());
        inventoryMovementRepository.save(movement);
    }

    private void publishEvent(Inventory inventory, String eventType, int quantity) {
        outboxPublisher.publish(inventoryTopic, "INVENTORY", String.valueOf(inventory.getProductId()),
                eventType,
                new InventoryEvent(inventory.getProductId(), eventType, quantity, inventory.getStock()));
    }
}
