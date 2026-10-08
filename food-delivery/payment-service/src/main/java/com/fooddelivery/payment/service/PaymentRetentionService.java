package com.fooddelivery.payment.service;

import com.fooddelivery.payment.repository.PaymentRepository;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 보존 기간이 지난 결제를 한 묶음씩 지운다. 지우는 것은 끝난 결제뿐이다.
 * <ul>
 *   <li>FAILED — 기준 시각은 처리 시각(processed_at, 없으면 created_at)이다.</li>
 *   <li>정산 대상 상태(SettlementBatch.SETTLEABLE_STATUSES: APPROVED·COMPLETED·SUCCESS) 중 정산이
 *       끝난 것(settled_at 이 있는 것) — 기준 시각은 정산 시각(settled_at)이다.</li>
 * </ul>
 * PENDING 과 아직 정산되지 않은 승인 결제는 진행 중이라 남긴다(정산 배치가 읽는 행이다). 그 밖에
 * 코드가 모르는 상태도 남긴다 — 모르는 것을 끝났다고 가정하지 않는다.
 */
@Service
public class PaymentRetentionService {

    static final String FAILED_STATUS = "FAILED";

    private final PaymentRepository paymentRepository;

    public PaymentRetentionService(PaymentRepository paymentRepository) {
        this.paymentRepository = paymentRepository;
    }

    /** 한 묶음의 결과. resumeAfterId 가 0 이면 다음 묶음은 처음(가장 오래된 행)부터 다시 본다. */
    public record Result(int purged, long resumeAfterId) {
    }

    /**
     * id 가 afterId 보다 큰 결제를 id 순으로 batchSize 건 읽어, 그중 끝난 지 보존 기간이 지난
     * 결제만 지운다. 결제를 가리키는 FK 는 없다(payments.order_id 도 FK 가 아니다).
     *
     * <p>커서: 묶음의 마지막 행이 생성된 지 아직 보존 기간 안이거나 묶음이 덜 찼으면 끝까지 본
     * 것이라 0 으로 되돌린다(끝난 시각은 생성 시각보다 늦으므로 그 뒤 행은 대상이 될 수 없다).
     * 그 밖에는 마지막 id 를 돌려줘 다음 묶음이 이어 읽게 한다.
     *
     * <p>이 커서는 id 순서가 시각 순서와 같다고 본다. 런타임 행은 IDENTITY 와 now() 가 함께 늘고,
     * id 앞쪽의 시드 행은 모두 런타임 행보다 오래됐다. 가정이 깨져 보존 기간 안의 행이 앞쪽에 있으면
     * 그 행이 기간을 넘길 때까지 정리가 거기서 쉰다 — 덜 지우는 쪽으로만 틀린다.
     */
    @Transactional
    public Result purgeOlderThan(int retentionDays, long afterId, int batchSize) {
        LocalDateTime cutoff = LocalDateTime.now().minusDays(retentionDays);
        List<PaymentRepository.RetentionHead> heads =
                paymentRepository.findRetentionHeads(afterId, PageRequest.of(0, batchSize));
        if (heads.isEmpty()) {
            return new Result(0, 0L);
        }
        List<Long> ids = heads.stream()
                .filter(head -> {
                    LocalDateTime finishedAt = finishedAt(head);
                    return finishedAt != null && finishedAt.isBefore(cutoff);
                })
                .map(PaymentRepository.RetentionHead::getId)
                .toList();
        if (!ids.isEmpty()) {
            paymentRepository.deleteAllByIdInBatch(ids);
        }
        PaymentRepository.RetentionHead last = heads.get(heads.size() - 1);
        boolean reachedRecent = heads.size() < batchSize
                || (last.getCreatedAt() != null && !last.getCreatedAt().isBefore(cutoff));
        return new Result(ids.size(), reachedRecent ? 0L : last.getId());
    }

    /** 끝난 결제면 끝난 시각을, 진행 중이거나 모르는 상태면 null 을 돌려준다. */
    static LocalDateTime finishedAt(PaymentRepository.RetentionHead head) {
        if (FAILED_STATUS.equals(head.getStatus())) {
            return head.getProcessedAt() != null ? head.getProcessedAt() : head.getCreatedAt();
        }
        if (SettlementBatch.SETTLEABLE_STATUSES.contains(head.getStatus())) {
            return head.getSettledAt();
        }
        return null;
    }
}
