package com.fooddelivery.order.service;

import com.fooddelivery.order.repository.OrderItemRepository;
import com.fooddelivery.order.repository.OrderRepository;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Set;

/**
 * 보존 기간이 지난 주문과 그 주문 항목을 한 묶음씩 지운다. 지우는 것은 끝난 주문(DELIVERED,
 * CANCELLED)뿐이다. PENDING 은 진행 중이라 남긴다 — 결제·배차가 끝내 끝나지 않은 묵은 PENDING 은
 * OrderCleanupBatch 가 order.cleanup.retention-days(30일) 뒤 CANCELLED 로 바꾸고, 그다음 주기에
 * 여기서 지워진다.
 *
 * <p>기준 시각은 created_at 이다. orders 에는 종료 시각 컬럼이 없다.
 */
@Service
public class OrderRetentionService {

    static final Set<String> FINISHED_STATUSES = Set.of("DELIVERED", "CANCELLED");

    private final OrderRepository orderRepository;
    private final OrderItemRepository orderItemRepository;

    public OrderRetentionService(OrderRepository orderRepository, OrderItemRepository orderItemRepository) {
        this.orderRepository = orderRepository;
        this.orderItemRepository = orderItemRepository;
    }

    /** 한 묶음의 결과. resumeAfterId 가 0 이면 다음 묶음은 처음(가장 오래된 행)부터 다시 본다. */
    public record Result(int purged, int purgedItems, long resumeAfterId) {
    }

    /**
     * id 가 afterId 보다 큰 주문을 id 순으로 batchSize 건 읽어, 그중 보존 기간이 지난 끝난 주문만
     * 항목과 함께 지운다. 항목을 먼저 지운다 — fk_order_items_order 에 ON DELETE 가 없어 순서가
     * 바뀌면 주문 삭제가 FK 위반으로 실패한다. 두 문장은 한 트랜잭션이다.
     *
     * <p>커서: 묶음의 마지막 행이 아직 보존 기간 안이거나 묶음이 덜 찼으면 끝까지 본 것이라 0 으로
     * 되돌린다. 그 밖에는 마지막 id 를 돌려줘 다음 묶음이 이어 읽게 한다 — 남겨 둔 PENDING 이
     * 앞머리에 쌓여도 정리가 그 자리에 멈추지 않는다.
     *
     * <p>이 커서는 id 순서가 시각 순서와 같다고 본다. 런타임 행은 IDENTITY 와 now() 가 함께 늘고,
     * id 앞쪽의 시드 행은 모두 런타임 행보다 오래됐다. 가정이 깨져 보존 기간 안의 행이 앞쪽에 있으면
     * 그 행이 기간을 넘길 때까지 정리가 거기서 쉰다 — 덜 지우는 쪽으로만 틀린다.
     */
    @Transactional
    public Result purgeOlderThan(int retentionDays, long afterId, int batchSize) {
        LocalDateTime cutoff = LocalDateTime.now().minusDays(retentionDays);
        List<OrderRepository.RetentionHead> heads =
                orderRepository.findRetentionHeads(afterId, PageRequest.of(0, batchSize));
        if (heads.isEmpty()) {
            return new Result(0, 0, 0L);
        }
        List<Long> ids = heads.stream()
                .filter(head -> FINISHED_STATUSES.contains(head.getStatus())
                        && head.getCreatedAt() != null && head.getCreatedAt().isBefore(cutoff))
                .map(OrderRepository.RetentionHead::getId)
                .toList();
        int items = 0;
        if (!ids.isEmpty()) {
            items = orderItemRepository.deleteByOrderIdIn(ids);
            orderRepository.deleteAllByIdInBatch(ids);
        }
        OrderRepository.RetentionHead last = heads.get(heads.size() - 1);
        boolean reachedRecent = heads.size() < batchSize
                || (last.getCreatedAt() != null && !last.getCreatedAt().isBefore(cutoff));
        return new Result(ids.size(), items, reachedRecent ? 0L : last.getId());
    }
}
