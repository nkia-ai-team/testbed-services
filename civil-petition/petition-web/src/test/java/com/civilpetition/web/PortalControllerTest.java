package com.civilpetition.web;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.dao.QueryTimeoutException;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockHttpSession;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest({PortalController.class, AdminBatchController.class})
class PortalControllerTest {

    @Autowired
    MockMvc mvc;

    @MockitoBean
    PetitionRepository petitions;

    @MockitoBean
    BatchService batches;

    private static MockHttpSession session(String role, Integer dept) {
        MockHttpSession session = new MockHttpSession();
        session.setAttribute(SessionUser.KEY, new SessionUser(7L, role, dept));
        return session;
    }

    @Test
    void submitNeedsALoggedInUser() throws Exception {
        mvc.perform(post("/api/petitions").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"deptId\":1,\"category\":\"road\",\"title\":\"t\",\"body\":\"b\"}"))
                .andExpect(status().isUnauthorized());
        verify(petitions, never()).create(anyLong(), anyInt(), anyString(), anyString(), anyString(), anyBoolean(), any());
    }

    @Test
    void submitWritesThePetitionForTheSessionUser() throws Exception {
        when(petitions.create(eq(7L), eq(3), eq("road"), eq("pothole"), eq("please fix"), eq(true), any()))
                .thenReturn(Map.of("receipt_no", "CP02000001", "status", "RECEIVED"));
        mvc.perform(post("/api/petitions").session(session("CITIZEN", null)).contentType(MediaType.APPLICATION_JSON)
                        .content("{\"deptId\":3,\"category\":\"road\",\"title\":\"pothole\",\"body\":\"please fix\",\"isPublic\":true,"
                                + "\"attachments\":[{\"fileName\":\"a.jpg\",\"sizeBytes\":1200}]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.receipt_no").value("CP02000001"));
    }

    @Test
    void lookupRejectsMalformedAndReportsMissingReceipts() throws Exception {
        mvc.perform(get("/api/petitions/XX1")).andExpect(status().isBadRequest());
        when(petitions.findByReceipt("CP00000009")).thenReturn(Optional.empty());
        mvc.perform(get("/api/petitions/CP00000009")).andExpect(status().isNotFound());
    }

    @Test
    void loginComparesTheSaltedHashAndStartsASession() throws Exception {
        Map<String, Object> user = new HashMap<>();
        user.put("user_id", 7L);
        user.put("password_hash", PortalController.hash("citizen00007", "pw"));
        user.put("role", "CITIZEN");
        user.put("dept_id", null);
        when(petitions.findUser("citizen00007")).thenReturn(Optional.of(user));
        mvc.perform(post("/api/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"loginId\":\"citizen00007\",\"password\":\"pw\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.userId").value(7));
        mvc.perform(post("/api/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"loginId\":\"citizen00007\",\"password\":\"nope\"}"))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void staffPathsRejectCitizens() throws Exception {
        mvc.perform(get("/api/staff/petitions").session(session("CITIZEN", null))).andExpect(status().isForbidden());
        when(petitions.inbox(5, 10)).thenReturn(List.of(Map.of("petition_id", 11L)));
        mvc.perform(get("/api/staff/petitions").session(session("STAFF", 5)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].petition_id").value(11));
    }

    @Test
    void searchBoundsTheDateRange() throws Exception {
        mvc.perform(get("/api/public/petitions").param("keyword", "road").param("from", "2026-01-01").param("to", "2026-12-31"))
                .andExpect(status().isBadRequest());
        mvc.perform(get("/api/public/petitions").param("keyword", "road").param("from", "2026-10-01").param("to", "2026-10-31"))
                .andExpect(status().isOk());
    }

    @Test
    void databaseFailuresAnswer503() throws Exception {
        when(petitions.findByReceipt("CP00000001")).thenThrow(new QueryTimeoutException("canceling statement due to statement timeout"));
        mvc.perform(get("/api/petitions/CP00000001")).andExpect(status().isServiceUnavailable());
    }

    @Test
    void batchJobsRunOnlyFromTheSameServer() throws Exception {
        mvc.perform(post("/admin/batch/stats").with(request -> {
            request.setRemoteAddr("192.0.2.10");
            return request;
        })).andExpect(status().isForbidden());
        verify(batches, never()).run(anyString());
        when(batches.run("stats")).thenReturn(Map.of("job", "stats", "rows", 600L));
        mvc.perform(post("/admin/batch/stats").with(request -> {
            request.setRemoteAddr("127.0.0.1");
            return request;
        })).andExpect(status().isOk()).andExpect(jsonPath("$.rows").value(600));
    }
}
