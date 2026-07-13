package com.commerce.inventory.service;

import com.commerce.inventory.entity.Inventory;
import com.commerce.inventory.entity.InventoryMovement;
import com.commerce.inventory.repository.InventoryMovementRepository;
import com.commerce.inventory.repository.InventoryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

/**
 * 재고 재조정(reconciliation) 배치 — 10분마다 각 상품의 inventory_movements 최신 원장값과
 * 현재 inventory.stock을 대사한다. 불일치는 out-of-band 변경(수동 보정, 버그 등)의 신호이므로
 * WARN 로그로 남기고, 원장 쪽에 현재 stock을 반영하는 보정 ADJUST 행을 추가해 다음 대사부터는
 * 다시 일치하게 만든다(라이브 stock 값 자체는 신뢰하고 건드리지 않는다).
 */
@Component
public class ReconciliationBatch {

    private static final Logger log = LoggerFactory.getLogger(ReconciliationBatch.class);

    private final InventoryRepository inventoryRepository;
    private final InventoryMovementRepository inventoryMovementRepository;

    public ReconciliationBatch(InventoryRepository inventoryRepository,
                                InventoryMovementRepository inventoryMovementRepository) {
        this.inventoryRepository = inventoryRepository;
        this.inventoryMovementRepository = inventoryMovementRepository;
    }

    @Scheduled(fixedDelayString = "${inventory.reconciliation.interval-ms:600000}")
    @Transactional
    public void run() {
        List<Inventory> all = inventoryRepository.findAll();
        log.info("Reconciliation batch started: productsChecked={}", all.size());

        int mismatchCount = 0;
        for (Inventory inventory : all) {
            var last = inventoryMovementRepository.findTopByProductIdOrderByCreatedAtDesc(inventory.getProductId());
            if (last.isEmpty()) {
                continue; // 아직 movement 이력이 없는 상품 — 대사 대상 아님
            }
            int ledgerStock = last.get().getResultingStock();
            if (ledgerStock != inventory.getStock()) {
                mismatchCount++;
                log.warn("Inventory mismatch detected: productId={}, ledgerStock={}, actualStock={} — recording corrective ADJUST",
                        inventory.getProductId(), ledgerStock, inventory.getStock());

                InventoryMovement corrective = new InventoryMovement();
                corrective.setProductId(inventory.getProductId());
                corrective.setMovementType("ADJUST");
                corrective.setQuantity(inventory.getStock() - ledgerStock);
                corrective.setResultingStock(inventory.getStock());
                inventoryMovementRepository.save(corrective);
            }
        }

        log.info("Reconciliation batch finished: productsChecked={}, mismatches={}", all.size(), mismatchCount);
    }
}
