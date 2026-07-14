package com.commerce.inventory.service;

import com.commerce.inventory.entity.Inventory;
import com.commerce.inventory.entity.InventoryMovement;
import com.commerce.inventory.repository.InventoryMovementRepository;
import com.commerce.inventory.repository.InventoryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

/**
 * 재고 재조정(reconciliation) 배치 — 10분마다 각 상품의 inventory_movements 최신 원장값과
 * 현재 inventory.stock을 대사한다. 불일치는 out-of-band 변경(수동 보정, 버그 등)의 신호이므로
 * WARN 로그로 남기고, 원장 쪽에 현재 stock을 반영하는 보정 ADJUST 행을 추가해 다음 대사부터는
 * 다시 일치하게 만든다(라이브 stock 값 자체는 신뢰하고 건드리지 않는다).
 *
 * 대사에 이어 기준치 미달 재고를 보충(RESTOCK, 입고 모사)한다 — 상주 부하(loadgen)가
 * 재고를 계속 소비하므로 보충이 없으면 장기 실행 시 전 상품이 소진돼 checkout 여정이
 * 영구 실패한다(자립성, spec-testbed-expansion §8. 2026-07-14 실측으로 확인된 버그).
 */
@Component
public class ReconciliationBatch {

    private static final Logger log = LoggerFactory.getLogger(ReconciliationBatch.class);

    private final InventoryRepository inventoryRepository;
    private final InventoryMovementRepository inventoryMovementRepository;
    private final int restockThreshold;
    private final int restockTarget;

    public ReconciliationBatch(InventoryRepository inventoryRepository,
                                InventoryMovementRepository inventoryMovementRepository,
                                @Value("${inventory.restock.threshold:20}") int restockThreshold,
                                @Value("${inventory.restock.target:200}") int restockTarget) {
        this.inventoryRepository = inventoryRepository;
        this.inventoryMovementRepository = inventoryMovementRepository;
        this.restockThreshold = restockThreshold;
        this.restockTarget = restockTarget;
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

        int restockCount = 0;
        for (Inventory inventory : all) {
            if (inventory.getStock() >= restockThreshold) {
                continue;
            }
            int quantity = restockTarget - inventory.getStock();
            inventory.setStock(restockTarget);
            inventoryRepository.save(inventory);

            InventoryMovement restock = new InventoryMovement();
            restock.setProductId(inventory.getProductId());
            restock.setMovementType("RESTOCK");
            restock.setQuantity(quantity);
            restock.setResultingStock(restockTarget);
            inventoryMovementRepository.save(restock);

            restockCount++;
            log.info("Restocked productId={}: +{} -> {}", inventory.getProductId(), quantity, restockTarget);
        }

        log.info("Reconciliation batch finished: productsChecked={}, mismatches={}, restocked={}",
                all.size(), mismatchCount, restockCount);
    }
}
