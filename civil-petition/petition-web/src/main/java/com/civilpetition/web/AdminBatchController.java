package com.civilpetition.web;

import jakarta.servlet.http.HttpServletRequest;
import java.util.Map;
import java.util.Set;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

/** 배치 관리 경로. 같은 서버(루프백)에서 온 요청만 받는다. 웹 앞단을 지나온 요청은 거절한다. */
@RestController
public class AdminBatchController {

    static final Set<String> LOOPBACK = Set.of("127.0.0.1", "0:0:0:0:0:0:0:1", "::1");
    private final BatchService batches;

    public AdminBatchController(BatchService batches) {
        this.batches = batches;
    }

    @PostMapping("/admin/batch/{job}")
    public Map<String, Object> run(@PathVariable String job, HttpServletRequest request) {
        if (!LOOPBACK.contains(request.getRemoteAddr())) {
            throw new ApiException(HttpStatus.FORBIDDEN, "local only");
        }
        return batches.run(job);
    }
}
