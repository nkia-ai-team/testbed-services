package com.fooddelivery.dispatch.service;

import com.fooddelivery.dispatch.entity.Dispatch;
import com.fooddelivery.dispatch.event.DispatchOutboxPublisher;
import com.fooddelivery.dispatch.repository.DispatchEventLogRepository;
import com.fooddelivery.dispatch.repository.DispatchRepository;
import org.junit.jupiter.api.Test;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.Slice;
import org.springframework.data.domain.SliceImpl;
import org.springframework.data.repository.query.parser.PartTree;

import java.util.Arrays;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * GET /api/deliveries 는 부하 스크립트가 쉬지 않고 부른다(status=DELIVERED, size=20). 페이지마다
 * 전체 건수를 세면 그 COUNT 가 끝난 배차 수백만 행을 훑어 Hikari 풀을 말렸다(2026-10-08 실측 평균 최대
 * 5.4초, 부하 때 15.4초). 응답은 배열이라 건수가 필요 없다. 그래서 목록 쿼리가 Slice 를 돌려주는지
 * (Spring Data 가 COUNT 를 내지 않는다), status 유무에 따라 어느 쿼리를 쓰는지를 고정한다.
 */
class DispatchSearchTest {

    private final DispatchRepository dispatches = mock(DispatchRepository.class);
    private final DispatchService service = new DispatchService(dispatches, mock(DispatchEventLogRepository.class),
            mock(DispatchOutboxPublisher.class), 2000, "food.dispatch");

    private static Slice<Dispatch> slice(Dispatch... rows) {
        return new SliceImpl<>(List.of(rows));
    }

    @Test
    void a_status_filter_goes_to_the_derived_status_query() {
        Dispatch d = new Dispatch();
        d.setStatus("DELIVERED");
        when(dispatches.findByStatus(anyString(), any())).thenReturn(slice(d));

        assertEquals(1, service.searchDispatches("DELIVERED", PageRequest.of(0, 20)).size());

        verify(dispatches).findByStatus("DELIVERED", PageRequest.of(0, 20));
        verify(dispatches, never()).findAllBy(any());
    }

    @Test
    void no_status_filter_goes_to_the_unfiltered_query() {
        when(dispatches.findAllBy(any())).thenReturn(slice());

        service.searchDispatches(null, Pageable.unpaged());

        verify(dispatches).findAllBy(Pageable.unpaged());
        verify(dispatches, never()).findByStatus(anyString(), any());
    }

    @Test
    void list_queries_return_slices_so_spring_data_issues_no_count_query() throws Exception {
        assertEquals(Slice.class,
                DispatchRepository.class.getMethod("findByStatus", String.class, Pageable.class).getReturnType());
        assertEquals(Slice.class, DispatchRepository.class.getMethod("findAllBy", Pageable.class).getReturnType());
        // 옛 OR-null 검색이 되살아나지 않게 한다.
        assertFalse(Arrays.stream(DispatchRepository.class.getMethods()).anyMatch(m -> m.getName().equals("search")));
    }

    @Test
    void the_derived_query_names_parse_to_the_intended_predicates() {
        PartTree byStatus = new PartTree("findByStatus", Dispatch.class);
        assertEquals(1, byStatus.getParts().toList().size());
        assertEquals("status", byStatus.getParts().toList().get(0).getProperty().getSegment());

        PartTree all = new PartTree("findAllBy", Dispatch.class);
        assertEquals(0, all.getParts().toList().size());
    }
}
