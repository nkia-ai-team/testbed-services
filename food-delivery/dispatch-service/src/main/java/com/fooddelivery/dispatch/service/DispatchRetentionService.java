package com.fooddelivery.dispatch.service;

import com.fooddelivery.dispatch.repository.DispatchEventLogRepository;
import com.fooddelivery.dispatch.repository.DispatchRepository;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 보존 기간이 지난 배차와 그 전이 이력을 한 묶음씩 지운다. 지우는 것은 끝난 배차(DELIVERED)뿐이다
 * — ASSIGNED 는 용량 게이트(countByStatus)와 만료 배치가 보는 진행 중 상태라 아무리 오래돼도 남긴다.
 *
 * <p>기준 시각은 assigned_at 이다. 배달 완료 시각 컬럼은 따로 없고 완료는 배정 뒤 ETA(15~34분)
 * 안에 일어나므로, 30일 보존에서 둘의 차이는 의미가 없다.
 */
@Service
public class DispatchRetentionService {

    static final String FINISHED_STATUS = "DELIVERED";

    private final DispatchRepository dispatchRepository;
    private final DispatchEventLogRepository dispatchEventLogRepository;

    public DispatchRetentionService(DispatchRepository dispatchRepository,
                                    DispatchEventLogRepository dispatchEventLogRepository) {
        this.dispatchRepository = dispatchRepository;
        this.dispatchEventLogRepository = dispatchEventLogRepository;
    }

    /** 한 묶음의 결과. resumeAfterId 가 0 이면 다음 묶음은 처음(가장 오래된 행)부터 다시 본다. */
    public record Result(int purged, int purgedEvents, long resumeAfterId) {
    }

    /**
     * id 가 afterId 보다 큰 배차를 id 순으로 batchSize 건 읽어, 그중 보존 기간이 지난 DELIVERED
     * 배차만 이력과 함께 지운다. 이력에는 FK 가 없지만 배차보다 먼저 지워 고아 이력이 남는 순간을
     * 만들지 않는다.
     *
     * <p>커서: 묶음의 마지막 행이 아직 보존 기간 안이거나 묶음이 덜 찼으면 끝까지 본 것이라 0 으로
     * 되돌린다. 그 밖에는 마지막 id 를 돌려줘 다음 묶음이 이어 읽게 한다 — 남겨 둔 진행 중 행이
     * 앞머리에 쌓여도 정리가 그 자리에 멈추지 않는다.
     *
     * <p>이 커서는 id 순서가 시각 순서와 같다고 본다. 런타임 행은 IDENTITY 와 now() 가 함께 늘고,
     * id 앞쪽의 시드 행은 모두 런타임 행보다 오래됐다. 가정이 깨져 보존 기간 안의 행이 앞쪽에 있으면
     * 그 행이 기간을 넘길 때까지 정리가 거기서 쉰다 — 덜 지우는 쪽으로만 틀린다.
     */
    @Transactional
    public Result purgeOlderThan(int retentionDays, long afterId, int batchSize) {
        LocalDateTime cutoff = LocalDateTime.now().minusDays(retentionDays);
        List<DispatchRepository.RetentionHead> heads =
                dispatchRepository.findRetentionHeads(afterId, PageRequest.of(0, batchSize));
        if (heads.isEmpty()) {
            return new Result(0, 0, 0L);
        }
        List<Long> ids = heads.stream()
                .filter(head -> FINISHED_STATUS.equals(head.getStatus())
                        && head.getAssignedAt() != null && head.getAssignedAt().isBefore(cutoff))
                .map(DispatchRepository.RetentionHead::getId)
                .toList();
        int events = 0;
        if (!ids.isEmpty()) {
            events = dispatchEventLogRepository.deleteByDispatchIdIn(ids);
            dispatchRepository.deleteAllByIdInBatch(ids);
        }
        DispatchRepository.RetentionHead last = heads.get(heads.size() - 1);
        boolean reachedRecent = heads.size() < batchSize
                || (last.getAssignedAt() != null && !last.getAssignedAt().isBefore(cutoff));
        return new Result(ids.size(), events, reachedRecent ? 0L : last.getId());
    }
}
