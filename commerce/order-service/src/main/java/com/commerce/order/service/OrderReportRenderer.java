package com.commerce.order.service;

import org.springframework.stereotype.Component;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;

/**
 * 주문 리포트 렌더러.
 *
 * <p>렌더 결과를 조립할 때 내부 버퍼({@link #buffer})를 재사용한다. 버퍼가 인스턴스
 * 상태이므로 이 클래스는 스레드 안전하지 않고, 그래서 {@link #render(int)} 전체가
 * {@code synchronized}다 — 즉 <b>동시 요청이 이 모니터 하나에서 직렬화된다.</b>
 *
 * <p>렌더 비용은 요청 구간 길이에 비례하지 않고 <b>제곱으로</b> 증가한다.
 * 일별 이동평균을 만들면서 각 날짜마다 창 전체를 다시 훑기 때문이다
 * ({@link #movingAverages}). 구간이 길어질수록 한 번의 렌더가 길어지고, 렌더가
 * 직렬화되어 있으므로 처리율 상한이 곧 {@code 1 / 렌더시간}이 된다. 도착률이 그
 * 상한을 넘으면 초과 요청은 모니터 앞에 줄을 서고, 줄 선 요청은 서블릿 워커 스레드를
 * 쥔 채 기다리므로 Tomcat 워커 풀이 고갈된다.
 *
 * <p>DB에 닿지 않는다는 점이 중요하다. 커넥션 풀도 DB 경합도 개입하지 않으므로
 * 스레드 고갈이 다른 자원 문제와 섞이지 않는다.
 */
@Component
public class OrderReportRenderer {

    /** 하루를 시간 단위로 쪼개 집계한다. */
    private static final int BUCKETS_PER_DAY = 24;

    /** 재사용되는 조립 버퍼 — 이 인스턴스 상태가 클래스를 스레드 안전하지 않게 만든다. */
    private final StringBuilder buffer = new StringBuilder();

    public synchronized String render(int days) {
        buffer.setLength(0);

        List<double[]> series = buildSeries(days);
        double[] averages = movingAverages(series);

        LocalDate start = LocalDate.now().minusDays(days);
        buffer.append("order-report days=").append(days).append('\n');
        for (int i = 0; i < averages.length; i++) {
            buffer.append(start.plusDays(i / BUCKETS_PER_DAY))
                  .append(" bucket=").append(i % BUCKETS_PER_DAY)
                  .append(" avg=").append(String.format("%.4f", averages[i]))
                  .append('\n');
        }
        return buffer.toString();
    }

    private List<double[]> buildSeries(int days) {
        int points = days * BUCKETS_PER_DAY;
        List<double[]> series = new ArrayList<>(points);
        for (int i = 0; i < points; i++) {
            // 시간대별 주문 곡선 근사 — 낮에 높고 새벽에 낮다.
            double hourOfDay = i % BUCKETS_PER_DAY;
            double seasonal = 1.0 + Math.sin((hourOfDay / BUCKETS_PER_DAY) * 2 * Math.PI);
            series.add(new double[]{i, seasonal * (100 + (i % 37))});
        }
        return series;
    }

    /**
     * 각 지점의 이동평균. 창을 슬라이딩하지 않고 지점마다 처음부터 다시 더하므로
     * 비용이 O(n^2)다 — 짧은 구간에서는 눈에 띄지 않다가 구간이 길어지면 급격히 나빠진다.
     */
    private double[] movingAverages(List<double[]> series) {
        int n = series.size();
        double[] out = new double[n];
        for (int i = 0; i < n; i++) {
            double sum = 0;
            int count = 0;
            for (int j = 0; j <= i; j++) {
                sum += series.get(j)[1];
                count++;
            }
            out[i] = sum / count;
        }
        return out;
    }
}
