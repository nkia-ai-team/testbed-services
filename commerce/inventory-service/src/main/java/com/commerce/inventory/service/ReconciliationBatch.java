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
 * 재고 재조정(reconciliation) 배치 — 1분마다 각 상품의 inventory_movements 최신 원장값과
 * 현재 inventory.stock을 대사한다. 불일치는 out-of-band 변경(수동 보정, 버그 등)의 신호이므로
 * WARN 로그로 남기고, 원장 쪽에 현재 stock을 반영하는 보정 ADJUST 행을 추가해 다음 대사부터는
 * 다시 일치하게 만든다(라이브 stock 값 자체는 신뢰하고 건드리지 않는다).
 *
 * 대사에 이어 기준치 미달 재고를 보충(RESTOCK, 입고 모사)한다 — 상주 부하(loadgen)가
 * 재고를 계속 소비하므로 보충이 없으면 장기 실행 시 전 상품이 소진돼 checkout 여정이
 * 영구 실패한다(자립성, spec-testbed-expansion §8. 2026-07-14 실측으로 확인된 버그).
 *
 * <p>2026-08-07 재척도(주기 600s→60s, target 200→60, threshold 20 유지). 종전 값은 평균
 * 공급은 맞았지만 <b>버스트</b>였다 — 10분에 한 번만, 그것도 이미 20 미만인 상품만 채우므로
 * 주기 끝에 전 상품이 0에 머무는 구간이 생겼다(08:07 flagship 16종 전부 stock=0 실측,
 * 소비는 ~110 units/min). 그 0 구간이 checkout을 409로 막아 payment 이후 홉의 피해를
 * 통째로 가렸다(F05-R run 95b07798: 창구 내 checkout 142건 전부 409, payment_reqs 0).
 *
 * <p><b>세 파라미터는 서로 당긴다. 하나만 바꾸면 다른 데서 깨진다:</b>
 * <ol>
 *   <li><b>평시 0 미도달</b> — {@code threshold > 상품당수요 × 주기}. 상품당 수요는
 *       ~6.9 units/min(110/16)이므로 주기 60s에서 소비는 ~7 &lt; threshold 20.</li>
 *   <li><b>F23-R(리스톡 정지) 결정론</b> — 풀(16 × target)이 그 시나리오의 min_hold(12m)
 *       안에 말라야 한다. target 60이면 풀 960, 110/min으로 ~8.7분. 종전 target 200은
 *       풀 3200이라 ~29분이 걸려 18m timeout을 넘겼다 — 즉 F23-R은 '주입 시점이 톱니의
 *       낮은 구간이었나'라는 운에 기대고 있었다.</li>
 *   <li><b>F23-R 감별자 {@code restock-still-running}</b> — 그 조건의 룩백 창은 주기에
 *       맞춰 잡은 값이다. 주기를 줄이면 창도 함께 줄여야 한다(이번에 12m→5m).
 *       창이 주기에 비해 너무 넓으면 주입 직전·롤아웃 중에 쓰인 RESTOCK 행이 판정 시작
 *       시점까지 남아 감별자가 오발화한다.</li>
 * </ol>
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
                                @Value("${inventory.restock.target:60}") int restockTarget) {
        this.inventoryRepository = inventoryRepository;
        this.inventoryMovementRepository = inventoryMovementRepository;
        this.restockThreshold = restockThreshold;
        this.restockTarget = restockTarget;
    }

    // 기본값은 코드에만 둔다 — 배포 매니페스트 env로 올리면 F23-R이 k8s.env로 잡아둔
    // baseline 환경변수 배열이 달라져 승인된 계약이 깨진다. F23-R의 주입(SPRING_APPLICATION_JSON
    // 으로 interval-ms를 늘려 배치를 멈춘다)은 코드 기본값을 그대로 덮으므로 계속 동작한다.
    @Scheduled(fixedDelayString = "${inventory.reconciliation.interval-ms:60000}")
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
